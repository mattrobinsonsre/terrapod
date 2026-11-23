package ssh_host_keys

import (
	"fmt"
	"net"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/diag"
)

// aHost is one validated entry from `hosts`.
type aHost struct {
	// key is the configured entry with surrounding whitespace removed. It is
	// the key of `entries` and the member of `unreachable_hosts`, so an
	// operator who wrote "web1.example.com:2222" indexes `entries` with the
	// string they wrote rather than with some normalised form of it.
	key string
	// address is the host:port form handed to the dialler.
	address string
}

// aRequest is everything one read needs, with the Terraform types already
// unwrapped and validated.
type aRequest struct {
	hosts    []aHost
	keyTypes []string
	timeout  time.Duration
}

// THE LINE BETWEEN AN ERROR AND A WARNING, in one place, because every refusal
// in this file is an instance of it: a value this data source cannot INTERPRET
// is a configuration mistake and fails the plan, where a host it can interpret
// and cannot REACH is a fact about the network and only warns.
//
// That asymmetry is the whole tolerance design. The hosts are expected to come
// from a CSP data source filtered to the instances that are running, and
// "running per the cloud" is not "reachable from wherever this provider is
// executing" -- a security group, an sshd that has not come up yet, an instance
// still in cloud-init. If any of those failed the plan, the first boot of a new
// fleet member would block every other host's key from being read. A typo in a
// port number has no such excuse.

// parseHosts unwraps and validates `hosts`.
func parseHosts(entries []string, defaultPort int64) ([]aHost, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := make([]aHost, 0, len(entries))
	// Keyed on the resolved address, so "web1" and "web1:22" are caught as the
	// same host even though a Terraform set holds them as two distinct members.
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
				fmt.Sprintf("Both resolve to %s, so this read would open the same connections twice and "+
					"report one host under two keys of `entries` — and a consumer looking a host up by "+
					"one spelling would silently miss the other. Keep one.", address))
			continue
		}
		byAddress[address] = entry
		out = append(out, aHost{key: entry, address: address})
	}

	// Sorted so the scan order, the order warnings are emitted in, and the
	// order entries are built in are all the same between reads. `hosts` is a
	// SET, which carries no order of its own, so without this the diagnostics
	// could reorder between two reads of an unchanged configuration.
	slices.SortFunc(out, func(a, b aHost) int { return strings.Compare(a.key, b.key) })
	return out, diags
}

// splitHostPort accepts "host", "host:port", "[host]:port" and a bare IPv6
// address.
//
// The bracket form is not only a convenience for somebody copying out of an
// ssh config: an IPv6 literal with a port REQUIRES it, because "::1:22" cannot
// be read as an address and a port without it.
func splitHostPort(entry string, defaultPort int64) (string, int64, error) {
	host, portText, splitErr := net.SplitHostPort(entry)
	if splitErr != nil {
		// SplitHostPort fails on a bare hostname ("missing port") and on a bare
		// IPv6 address ("too many colons"), both of which are legitimate: the
		// whole entry is the host and the default port applies.
		host, portText = entry, ""
	}
	host = strings.TrimSpace(host)
	// A bracketed host arrives from SplitHostPort already unwrapped, but a
	// bracketed entry with NO port ("[::1]") does not go through it at all, so
	// the brackets would otherwise survive into the dial address.
	host = strings.TrimPrefix(strings.TrimSuffix(host, "]"), "[")
	if host == "" {
		return "", 0, fmt.Errorf("no host before the port in %q", entry)
	}
	// None of these appears in a hostname, and the comma is the one that
	// matters: `hosts = ["web1,web2"]` is how a list written as a single string
	// arrives, and without this it would be dialled as a host literally called
	// "web1,web2" and reported unreachable -- a warning about the network for
	// what is a mistake in the configuration.
	if i := strings.IndexAny(host, " \t,#"); i >= 0 {
		return "", 0, fmt.Errorf("host %q contains %q, which does not appear in a hostname. If this is "+
			"meant to be several hosts, `hosts` is a set -- give each one its own entry",
			host, string(host[i]))
	}

	port := defaultPort
	// splitErr == nil means the entry carried a ":" that SplitHostPort read as
	// the port separator. So an EMPTY port after it is a typo rather than an
	// omission, and falling back to the default would quietly accept "web1:"
	// -- which is the one shape where "it worked" and "it did what I meant"
	// come apart without anything saying so.
	if splitErr == nil {
		n, convErr := strconv.ParseInt(portText, 10, 32)
		if convErr != nil {
			return "", 0, fmt.Errorf("port %q in %q is not a number. A port must be written as digits -- "+
				"a service name such as \"ssh\" is not resolved here. Omit the colon entirely to take "+
				"the data source's `port`", portText, entry)
		}
		port = n
	}
	if port < 1 || port > 65535 {
		return "", 0, fmt.Errorf("port %d in %q is outside 1-65535", port, entry)
	}
	return host, port, nil
}

// parseKeyTypes unwraps and validates `key_types`.
//
// An unknown algorithm is REFUSED rather than skipped. Skipping it would mean a
// configuration asking for one algorithm, spelled wrongly, read as a
// configuration asking for nothing -- so every host would come back with no
// keys and be reported unreachable, which points at the network instead of at
// the typo.
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
		// Deduped in the order asked for, because that order is what `entries`
		// lists a host's keys in and it has to be stable between reads.
		if slices.Contains(out, t) {
			continue
		}
		out = append(out, t)
	}
	return out, diags
}

// parseTimeout validates `scan_timeout_seconds`.
//
// It bounds ONE connection attempt rather than the read, which is the only
// bound that behaves predictably as a fleet grows: a whole-read budget divided
// across a worker pool would make each host's allowance depend on how many
// other hosts were configured, so adding a host could make an unrelated one
// start timing out.
func parseTimeout(seconds int64) (time.Duration, diag.Diagnostics) {
	var diags diag.Diagnostics
	if seconds < 1 {
		diags.AddError("`scan_timeout_seconds` must be at least 1",
			fmt.Sprintf("Got %d. It bounds a single connection attempt, and zero or less would abandon "+
				"every host before it answered -- reporting an entire fleet unreachable for a reason that "+
				"is in the configuration.", seconds))
		return 0, diags
	}
	return time.Duration(seconds) * time.Second, diags
}
