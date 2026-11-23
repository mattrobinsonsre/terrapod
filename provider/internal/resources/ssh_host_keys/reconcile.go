package ssh_host_keys

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// aHost is one validated entry from `hosts`.
type aHost struct {
	// key is the configured entry with surrounding whitespace removed. It is
	// the key of every map and the member of every set this resource emits, so
	// an operator who wrote "web1.example.com:2222" indexes `entries` with the
	// string they wrote.
	key string
	// address is the host:port form, which is what knownhosts.Line normalises
	// into a line's host field -- bare for port 22, "[host]:port" otherwise.
	address string
}

// aRequest is everything one read needs, with the Terraform types already
// unwrapped and validated.
type aRequest struct {
	hosts    []aHost
	keyTypes []string
	timeout  time.Duration
	// pinned is the hand-written lines from `pinned_entries`, per host key,
	// already validated as parseable known_hosts lines.
	pinned map[string][]string
}

// anOutcome is what one read decided.
type anOutcome struct {
	// entries is each host's pinned line(s), keyed as aHost.key.
	entries map[string][]string
	// body is the rendered known_hosts file.
	body string
	// unreachable is every host this read got no key out of, sorted.
	unreachable []string
	// changed is every host that answered with a key differing from the pinned
	// one for the same key type, sorted.
	changed []string
	// moved is true when this read pinned something it had not pinned before.
	// It is the only thing that advances scanned_at -- see the attribute's own
	// description for why that is not "whenever we scanned".
	moved bool
}

// THE RULE, in one place, because every branch below is an instance of it:
//
//	A host's pinned set is only ever established wholesale by a scan when there
//	is nothing pinned for it yet. After that a scan may ADD a key type that was
//	not pinned, and may never overwrite or remove one.
//
// That is what makes the pin worth having. Silently taking whatever answers on
// a later read is what SSH itself does after first contact, and it detects
// nothing; refusing to take it is the whole of the value this resource adds
// over `ansible_host_key_checking = false`.
//
// Three consequences worth stating, because each is a question somebody will
// ask of the code:
//
//   - `key_types` decides what the scan ASKS FOR. It does not decide what stays
//     pinned: narrowing it stops new types being added and un-pins nothing.
//     Removing trust is deliberate -- drop the host from `hosts`, or replace
//     the resource.
//   - A host in `pinned_entries` is never scanned at all. The operator has
//     declared what they already trust, which is a stronger statement than
//     anything a scan can make, so there is nothing for a scan to contribute.
//   - An unreachable host keeps what it had. That is not a convenience either:
//     a fleet where one host is rebooting must not have that host silently
//     fall out of the known_hosts file the rest of the fleet is verified
//     against, because the next configure would then fail on it for a reason
//     with nothing to do with its key.
func reconcile(
	ctx context.Context,
	req aRequest,
	prior map[string][]string,
	fetch hostKeyFetcher,
) (anOutcome, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := anOutcome{entries: map[string][]string{}}

	// Only the hosts a scan can say anything about are dialled.
	toScan := make([]aHost, 0, len(req.hosts))
	for _, h := range req.hosts {
		if _, declared := req.pinned[h.key]; !declared {
			toScan = append(toScan, h)
		}
	}
	results := scanAll(ctx, toScan, req.keyTypes, req.timeout, fetch)

	// Collected rather than reported per host: one diagnostic naming six
	// unreachable hosts is read, and six naming one each are scrolled past.
	var unreachableDetail, changedDetail, unparseable []string

	for _, h := range req.hosts {
		if lines, declared := req.pinned[h.key]; declared {
			out.entries[h.key] = lines
			if !slices.Equal(prior[h.key], lines) {
				out.moved = true
			}
			continue
		}

		res := results[h.key]
		priorLines := prior[h.key]

		if len(res.keys) == 0 {
			out.unreachable = append(out.unreachable, h.key)
			if len(priorLines) > 0 {
				out.entries[h.key] = slices.Clone(priorLines)
			}
			why := "no algorithm was attempted"
			if len(res.errs) > 0 {
				why = strings.Join(res.errs, "; ")
			}
			held := "nothing was pinned for it, so it is absent from known_hosts"
			if len(priorLines) > 0 {
				held = fmt.Sprintf("its %s kept from state", countedNoun(len(priorLines), "pinned entry", "pinned entries"))
			}
			unreachableDetail = append(unreachableDetail, fmt.Sprintf("  %s (%s) — %s; %s", h.key, h.address, why, held))
			continue
		}

		// Index what is already pinned by the key type each line carries, so a
		// scan can be compared against it type by type. The threat a pin
		// defends against is a DIFFERENT key for a type already pinned; a type
		// appearing for the first time is not that.
		kept := slices.Clone(priorLines)
		byType := make(map[string]string, len(priorLines))
		for _, line := range priorLines {
			t, err := keyTypeOfLine(line)
			if err != nil {
				// Kept, deliberately: state is not ours to discard. It takes
				// part in no type comparison, which is why it is reported --
				// an unparseable line is one a scan could sit a second entry
				// of the same type beside without noticing.
				unparseable = append(unparseable, fmt.Sprintf("  %s — %v", h.key, err))
				continue
			}
			byType[t] = line
		}

		for _, key := range res.keys {
			line := strings.TrimRight(knownhosts.Line([]string{h.address}, key), "\n")
			t := key.Type()
			if existing, pinnedAlready := byType[t]; pinnedAlready {
				if existing != line {
					out.changed = append(out.changed, h.key)
					changedDetail = append(changedDetail, fmt.Sprintf(
						"  %s (%s)\n    pinned:  %s\n    offered: %s", h.key, h.address, existing, line))
				}
				continue
			}
			kept = append(kept, line)
			byType[t] = line
			out.moved = true
		}
		out.entries[h.key] = kept
	}

	// A host can only be reported changed once however many of its key types
	// moved, and the set has to be sorted for state to be byte-stable.
	out.changed = sortedUnique(out.changed)
	out.unreachable = sortedUnique(out.unreachable)
	out.body = renderBody(out.entries)

	if len(unreachableDetail) > 0 {
		diags.AddWarning(
			// The deduped SET and not the detail list: a detail line is per
			// attempt, and the summary counts hosts.
			countedSummary(len(out.unreachable), "host", "could not be reached"),
			"This is not an error. A host that is down, firewalled off, or simply not up yet keeps "+
				"whatever was already pinned for it and is listed in `unreachable_hosts`; a host with nothing "+
				"pinned yet is left out of `known_hosts` until a later read reaches it.\n\n"+
				strings.Join(unreachableDetail, "\n")+
				"\n\nThe error text above distinguishes the cases this cannot: \"connection refused\" or a "+
				"timeout is a host problem, where \"no common algorithm for host key\" means the host is "+
				"up and holds none of the key types `key_types` asked for.\n\n"+
				"Where the provider runs matters here. In agent mode it runs on the runner, which is the "+
				"same network position the execution environment will connect from; in local mode it runs "+
				"on your own machine, which may not reach the fleet at all. See "+
				"docs/ansible-configure.md.")
	}
	if len(changedDetail) > 0 {
		diags.AddWarning(
			countedSummary(len(out.changed), "host", "presented a different host key"),
			"The pinned key has been KEPT and the offered one discarded, so `known_hosts` is unchanged "+
				"and the hosts below are listed in `changed_hosts`. Accepting the new key silently is what SSH "+
				"itself does after first contact, and it is the thing pinning exists to stop.\n\n"+
				strings.Join(changedDetail, "\n")+
				"\n\nA rebuilt host and an impersonated one look identical from here, so re-pinning is a "+
				"deliberate act. Either remove the host from `hosts` and add it back, which re-pins that "+
				"host alone, or run `terraform apply -replace=<this resource's address>`, which re-pins "+
				"every host it manages.")
	}
	if len(unparseable) > 0 {
		diags.AddWarning(
			"State holds a known_hosts line this resource cannot parse",
			"The line has been kept -- state is not this resource's to discard -- but it takes part in no "+
				"comparison, so a scan could pin a second entry of the same key type beside it without "+
				"reporting a change. Re-pin the host to clear it.\n\n"+strings.Join(unparseable, "\n"))
	}

	return out, diags
}

// renderBody writes the known_hosts file.
//
// Hosts are sorted and each host's lines keep the order they were pinned in, so
// two reads of an unchanged fleet produce byte-identical output. Without the
// sort the body would be a Go map's iteration order, which is deliberately
// randomised -- so every single plan would show the downstream variable
// changing, for a file whose content had not moved.
func renderBody(entries map[string][]string) string {
	hosts := make([]string, 0, len(entries))
	for h := range entries {
		hosts = append(hosts, h)
	}
	sort.Strings(hosts)

	var b strings.Builder
	for _, h := range hosts {
		for _, line := range entries[h] {
			b.WriteString(line)
			b.WriteByte('\n')
		}
	}
	return b.String()
}

// keyTypeOfLine returns the key type a known_hosts line carries.
//
// It parses with ssh.ParseKnownHosts rather than splitting on spaces, because
// the field a naive split would read as the key type is the second one only for
// the simplest shape: a `@cert-authority` or `@revoked` marker shifts every
// field along, and a line may carry several comma-separated hosts.
func keyTypeOfLine(line string) (string, error) {
	_, _, pub, _, _, err := ssh.ParseKnownHosts([]byte(line + "\n"))
	if err != nil {
		return "", fmt.Errorf("%q: %w", truncate(line), err)
	}
	return pub.Type(), nil
}

// parseHosts unwraps and validates `hosts`.
//
// Every refusal here is an ERROR rather than a warning, and the line between
// the two is whether the value can be interpreted at all: a host this resource
// cannot turn into an address is a configuration mistake, where a host it can
// address and cannot reach is a fact about the network.
func parseHosts(entries []string, defaultPort int64) ([]aHost, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := make([]aHost, 0, len(entries))
	// Keyed on the resolved address, so "web1" and "web1:22" are caught as the
	// same host even though a Terraform set holds them as two members.
	byAddress := map[string]string{}

	for _, raw := range entries {
		entry := strings.TrimSpace(raw)
		if entry == "" {
			diags.AddError("Empty host in `hosts`",
				"An entry of `hosts` is a hostname or address, optionally with `:port`. One entry is "+
					"empty or whitespace.")
			continue
		}
		host, port, err := splitHostPort(entry, defaultPort)
		if err != nil {
			diags.AddError(fmt.Sprintf("Malformed host %q", entry), err.Error())
			continue
		}
		address := net.JoinHostPort(host, strconv.FormatInt(port, 10))
		if first, clash := byAddress[address]; clash {
			diags.AddError(fmt.Sprintf("Two entries of `hosts` name the same host: %q and %q", first, entry),
				fmt.Sprintf("Both resolve to %s, so one of them would pin over the other and which won "+
					"would depend on set ordering. Keep one.", address))
			continue
		}
		byAddress[address] = entry
		out = append(out, aHost{key: entry, address: address})
	}

	// Sorted so the scan order, the diagnostics and the order new entries are
	// appended in are all the same between reads.
	slices.SortFunc(out, func(a, b aHost) int { return strings.Compare(a.key, b.key) })
	return out, diags
}

// splitHostPort accepts "host", "host:port" and "[v6]:port".
func splitHostPort(entry string, defaultPort int64) (string, int64, error) {
	host, portText, splitErr := net.SplitHostPort(entry)
	if splitErr != nil {
		// SplitHostPort fails on a bare hostname ("missing port") and on a bare
		// IPv6 address ("too many colons"), both of which are legitimate: the
		// whole entry is the host and the default port applies.
		host, portText = entry, ""
	}
	host = strings.TrimSpace(host)
	if host == "" {
		return "", 0, fmt.Errorf("no host before the port in %q", entry)
	}
	// A known_hosts line separates its hosts with commas and ends its host
	// field at the first space, and a `#` starts a comment -- so any of these
	// inside a host would produce a line that parses as something other than
	// what was asked for.
	if i := strings.IndexAny(host, " \t,#"); i >= 0 {
		return "", 0, fmt.Errorf("host %q contains %q, which is a separator in a known_hosts line",
			host, string(host[i]))
	}

	port := defaultPort
	// splitErr == nil means the entry carried a ":" that SplitHostPort read as
	// the port separator. So an EMPTY port after it is a typo rather than an
	// omission, and falling back to the default would quietly accept "web1:"
	// -- which is the one shape where "it worked" and "it did what I meant"
	// come apart without anything saying so.
	if splitErr == nil {
		if portText == "" {
			return "", 0, fmt.Errorf("port %q in %q is not a number. A port must be written as digits -- "+
				"a service name such as \"ssh\" is not resolved here. Omit the colon entirely to take "+
				"the resource's `port`", portText, entry)
		}
		n, convErr := strconv.ParseInt(portText, 10, 32)
		if convErr != nil {
			return "", 0, fmt.Errorf("port %q in %q is not a number. A port must be written as digits -- "+
				"a service name such as \"ssh\" is not resolved here", portText, entry)
		}
		port = n
	}
	if port < 1 || port > 65535 {
		return "", 0, fmt.Errorf("port %d in %q is outside 1-65535", port, entry)
	}
	return host, port, nil
}

// parseKeyTypes unwraps and validates `key_types`.
func parseKeyTypes(requested []string) ([]string, diag.Diagnostics) {
	var diags diag.Diagnostics
	if len(requested) == 0 {
		return slices.Clone(defaultKeyTypes), diags
	}
	out := make([]string, 0, len(requested))
	for _, t := range requested {
		if !slices.Contains(supportedKeyTypes, t) {
			diags.AddError(fmt.Sprintf("Unknown host-key algorithm %q", t),
				fmt.Sprintf("`key_types` names the SSH host-key algorithms to ask each host for. The "+
					"accepted values are %s.\n\nNote these are algorithm names, not `ssh-keyscan`'s short "+
					"`-t` aliases: write \"ssh-ed25519\" rather than \"ed25519\". `rsa-sha2-256` and "+
					"`rsa-sha2-512` are two signature algorithms over one `ssh-rsa` key, so asking for both "+
					"yields one entry.", strings.Join(supportedKeyTypes, ", ")))
			continue
		}
		if slices.Contains(out, t) {
			continue
		}
		out = append(out, t)
	}
	return out, diags
}

// parsePinned unwraps and validates `pinned_entries`.
//
// The line's OWN host field is deliberately not checked against the map key.
// known_hosts permits a comma-separated list, a `*` pattern and a hashed host
// there, so requiring it to equal the key would refuse lines that are correct.
// The key is how this resource addresses the entry; what ssh matches on is the
// line.
func parsePinned(raw map[string]string, hosts []aHost) (map[string][]string, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := make(map[string][]string, len(raw))

	declared := make(map[string]bool, len(hosts))
	for _, h := range hosts {
		declared[h.key] = true
	}

	var orphans []string
	for key, block := range raw {
		lines, err := parseKnownHostsBlock(block)
		if err != nil {
			diags.AddError(fmt.Sprintf("`pinned_entries[%q]` is not a known_hosts entry", key), err.Error())
			continue
		}
		out[key] = lines
		if !declared[key] {
			orphans = append(orphans, key)
		}
	}

	// A WARNING rather than an error, and the asymmetry with the refusals above
	// is deliberate. A pin for a host that is not in `hosts` is perfectly
	// interpretable -- it just has nothing to apply to -- and `hosts` is
	// routinely a set derived from an inventory, so a host legitimately leaving
	// the fleet would otherwise start failing every apply at the worst moment.
	// It is still said out loud, because an operator who pinned a key believes
	// that key is in the file.
	if len(orphans) > 0 {
		sort.Strings(orphans)
		diags.AddWarning("`pinned_entries` names hosts that are not in `hosts`",
			"These entries contribute nothing: this resource only emits lines for the hosts `hosts` "+
				"names.\n\n  "+strings.Join(orphans, "\n  ")+
				"\n\nAdd them to `hosts` if they should be in the file, or remove the pin.")
	}
	return out, diags
}

// parseKnownHostsBlock validates one `pinned_entries` value and returns its
// lines.
//
// A value may hold several lines, because one host legitimately offers several
// key types. Blank lines and comments are dropped; everything else is kept
// verbatim, so an operator's own `@cert-authority` marker or trailing comment
// survives into the file.
func parseKnownHostsBlock(block string) ([]string, error) {
	rest := []byte(strings.TrimSpace(block) + "\n")
	parsed := 0
	for {
		// ParseKnownHosts reports io.EOF for "nothing left to read", which is
		// the loop's exit rather than a failure -- so it has to be told apart
		// from a parse error before anything is concluded from err.
		_, _, _, _, remaining, err := ssh.ParseKnownHosts(rest)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w\n\nA value is one or more lines in known_hosts format: "+
				"\"<host> <keytype> <base64 key>\", which is what `ssh-keyscan` prints", err)
		}
		parsed++
		rest = remaining
	}
	if parsed == 0 {
		return nil, fmt.Errorf("it holds no entries -- only blank lines or comments. A value is one or " +
			"more lines in known_hosts format: \"<host> <keytype> <base64 key>\". To stop pinning a host, " +
			"remove its key from `pinned_entries` rather than blanking it")
	}

	var out []string
	for _, line := range strings.Split(block, "\n") {
		line = strings.TrimRight(line, " \t\r")
		if line == "" || strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}
		out = append(out, strings.TrimLeft(line, " \t"))
	}
	return out, nil
}

func sortedUnique(in []string) []string {
	if len(in) == 0 {
		// An empty slice rather than nil: a Terraform set reads an empty
		// collection as empty and a nil one as null, and a configuration
		// checking `length(...unreachable) == 0` should not have to also check
		// for null.
		return []string{}
	}
	out := slices.Clone(in)
	sort.Strings(out)
	return slices.Compact(out)
}

// countedSummary builds a diagnostic summary that agrees with itself about
// number: "1 host could not be reached", "3 hosts could not be reached".
func countedSummary(n int, noun, predicate string) string {
	if n == 1 {
		return fmt.Sprintf("1 %s %s", noun, predicate)
	}
	return fmt.Sprintf("%d %ss %s", n, noun, predicate)
}

// countedNoun agrees with itself about number in the middle of a sentence.
func countedNoun(n int, one, many string) string {
	if n == 1 {
		return "1 " + one
	}
	return fmt.Sprintf("%d %s", n, many)
}

// truncate keeps a quoted line in an error message readable. A base64 host key
// is several hundred characters and none of them help identify which line is
// meant.
func truncate(s string) string {
	const limit = 60
	if len(s) <= limit {
		return s
	}
	return s[:limit] + "…"
}
