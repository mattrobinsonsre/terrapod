package ssh_host_keys

import (
	"slices"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

// splitHostPort's behaviour is mostly net.SplitHostPort's, so what is pinned
// here is the handful of places this wrapper DIFFERS from it: the default port,
// the bracketed form with no port (which SplitHostPort rejects outright, so the
// brackets would otherwise survive into the dial address), the empty port after
// a colon, and the separator refusal.
func TestSplitHostPort(t *testing.T) {
	for _, tc := range []struct {
		name    string
		entry   string
		dflt    int64
		host    string
		port    int64
		wantErr string // a substring; "" means it must succeed
	}{
		{name: "a bare host takes the default port", entry: "web1", dflt: 22, host: "web1", port: 22},
		{name: "a non-default default is applied", entry: "web1", dflt: 2222, host: "web1", port: 2222},
		{name: "an explicit port overrides the default", entry: "web1:2222", dflt: 22, host: "web1", port: 2222},
		{name: "surrounding whitespace is tolerated", entry: " web1 ", dflt: 22, host: "web1", port: 22},

		// IPv6. The bracket form is REQUIRED to carry a port, because "::1:22"
		// cannot be read as an address plus a port -- and a bracketed entry
		// with no port never reaches net.SplitHostPort at all, so the brackets
		// have to be stripped here or they reach the dialler.
		{name: "a bare IPv6 literal takes the default port", entry: "2001:db8::1", dflt: 22,
			host: "2001:db8::1", port: 22},
		{name: "a bracketed IPv6 literal with a port", entry: "[2001:db8::1]:2222", dflt: 22,
			host: "2001:db8::1", port: 2222},
		{name: "a bracketed IPv6 literal with no port keeps neither bracket", entry: "[2001:db8::1]",
			dflt: 22, host: "2001:db8::1", port: 22},
		{name: "a bracketed hostname is accepted too", entry: "[web1]:2222", dflt: 22,
			host: "web1", port: 2222},

		// Refusals. Each is a value that cannot be INTERPRETED, which is the
		// line between an error and a warning in this package.
		{name: "an empty port after a colon is a typo, not an omission", entry: "web1:", dflt: 22,
			wantErr: "is not a number"},
		{name: "a service name is not resolved", entry: "web1:ssh", dflt: 22, wantErr: "is not a number"},
		{name: "port zero is out of range", entry: "web1:0", dflt: 22, wantErr: "outside 1-65535"},
		{name: "a port above 65535 is out of range", entry: "web1:70000", dflt: 22, wantErr: "outside 1-65535"},
		// ParseInt accepts the sign, so this reaches the range check rather
		// than the "not a number" branch -- and the range message is the more
		// useful of the two for a port that parsed fine and is nonsense.
		{name: "a negative port is out of range", entry: "web1:-1", dflt: 22, wantErr: "outside 1-65535"},
		{name: "nothing before the port", entry: ":22", dflt: 22, wantErr: "no host before the port"},
		// The comma is the one that matters: this is how a list written as a
		// single string arrives, and dialling it would report a configuration
		// mistake as a fact about the network.
		{name: "a comma-separated list in one entry is refused", entry: "web1,web2", dflt: 22,
			wantErr: "does not appear in a hostname"},
		{name: "an embedded space is refused", entry: "web1 web2", dflt: 22,
			wantErr: "does not appear in a hostname"},
		{name: "a pasted comment is refused", entry: "web1#old", dflt: 22,
			wantErr: "does not appear in a hostname"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			host, port, err := splitHostPort(tc.entry, tc.dflt)
			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("splitHostPort(%q) = (%q, %d, nil), want an error containing %q",
						tc.entry, host, port, tc.wantErr)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Errorf("error = %q, want it to contain %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("splitHostPort(%q): %v", tc.entry, err)
			}
			if host != tc.host || port != tc.port {
				t.Errorf("splitHostPort(%q) = (%q, %d), want (%q, %d)", tc.entry, host, port, tc.host, tc.port)
			}
		})
	}
}

func TestParseHosts(t *testing.T) {
	for _, tc := range []struct {
		name    string
		entries []string
		keys    []string // expected aHost.key values, in order
		addrs   []string // expected aHost.address values, in order
		wantErr string
	}{
		{
			// Sorted, because `hosts` is a SET and carries no order of its
			// own: without this the warnings could reorder between two reads
			// of an unchanged configuration.
			name:    "the targets come back sorted whatever order they arrived in",
			entries: []string{"web3", "web1", "web2"},
			keys:    []string{"web1", "web2", "web3"},
			addrs:   []string{"web1:22", "web2:22", "web3:22"},
		},
		{
			// The KEY is the entry as written and the ADDRESS is normalised:
			// one vocabulary for the consumer, another for the dialler.
			name:    "an entry keys entries exactly as written",
			entries: []string{"2001:db8::1", "web1:2222"},
			keys:    []string{"2001:db8::1", "web1:2222"},
			addrs:   []string{"[2001:db8::1]:22", "web1:2222"},
		},
		{
			name:    "whitespace is trimmed from the key as well as the address",
			entries: []string{"  web1  "},
			keys:    []string{"web1"},
			addrs:   []string{"web1:22"},
		},
		{
			name:    "an empty set is not an error",
			entries: nil,
			keys:    nil,
			addrs:   nil,
		},
		{
			name:    "an empty entry is refused",
			entries: []string{"web1", "   "},
			wantErr: "Empty host in `hosts`",
		},
		{
			// A Terraform set holds these as two distinct members, so nothing
			// upstream catches it -- and the consequence is not merely a
			// double scan: the same host appears under two keys of `entries`,
			// so a lookup by one spelling silently misses the other.
			name:    "two spellings of one host are refused",
			entries: []string{"web1", "web1:22"},
			wantErr: "name the same host",
		},
		{
			name:    "two spellings of one IPv6 host are refused",
			entries: []string{"2001:db8::1", "[2001:db8::1]:22"},
			wantErr: "name the same host",
		},
		{
			// Two entries that differ only by PORT are two targets, not a
			// clash: a host serving sshd twice is unusual and legitimate.
			name:    "the same host on two ports is two targets",
			entries: []string{"web1:22", "web1:2222"},
			keys:    []string{"web1:22", "web1:2222"},
			addrs:   []string{"web1:22", "web1:2222"},
		},
		{
			name:    "a malformed entry names itself",
			entries: []string{"web1:nope"},
			wantErr: `Malformed host "web1:nope"`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, diags := parseHosts(tc.entries, 22)
			if tc.wantErr != "" {
				if !diags.HasError() {
					t.Fatalf("parseHosts(%v) raised no error, want one containing %q", tc.entries, tc.wantErr)
				}
				if !strings.Contains(diagText(diags.Errors()), tc.wantErr) {
					t.Errorf("errors = %q, want one containing %q", diagText(diags.Errors()), tc.wantErr)
				}
				return
			}
			if diags.HasError() {
				t.Fatalf("parseHosts(%v): %s", tc.entries, diagText(diags.Errors()))
			}
			var keys, addrs []string
			for _, h := range got {
				keys = append(keys, h.key)
				addrs = append(addrs, h.address)
			}
			if !slices.Equal(keys, tc.keys) {
				t.Errorf("keys = %v, want %v", keys, tc.keys)
			}
			if !slices.Equal(addrs, tc.addrs) {
				t.Errorf("addresses = %v, want %v", addrs, tc.addrs)
			}
		})
	}
}

func TestParseKeyTypes(t *testing.T) {
	for _, tc := range []struct {
		name      string
		requested []string
		want      []string
		wantErr   string
	}{
		{
			name:      "nothing asked for takes the default",
			requested: nil,
			want:      defaultKeyTypes,
		},
		{
			// The order is the operator's and is preserved, because it is the
			// order `entries` lists a host's keys in -- so sorting here would
			// make the output disagree with the configuration for no gain.
			name:      "the order asked for is preserved",
			requested: []string{ssh.KeyAlgoRSASHA512, ssh.KeyAlgoED25519},
			want:      []string{ssh.KeyAlgoRSASHA512, ssh.KeyAlgoED25519},
		},
		{
			name:      "a repeat is dropped, keeping the first position",
			requested: []string{ssh.KeyAlgoED25519, ssh.KeyAlgoRSASHA512, ssh.KeyAlgoED25519},
			want:      []string{ssh.KeyAlgoED25519, ssh.KeyAlgoRSASHA512},
		},
		{
			name:      "every supported algorithm is accepted",
			requested: supportedKeyTypes,
			want:      supportedKeyTypes,
		},
		{
			// Refused rather than skipped. Skipping it would turn "one
			// algorithm, misspelled" into "no algorithms", so every host would
			// come back empty and be reported unreachable -- pointing at the
			// network instead of at the typo.
			name:      "an unknown algorithm is refused and named",
			requested: []string{"ed25519"},
			wantErr:   `Unknown host-key algorithm "ed25519"`,
		},
		{
			name:      "a DSA algorithm is not accepted",
			requested: []string{"ssh-dss"},
			wantErr:   `Unknown host-key algorithm "ssh-dss"`,
		},
		{
			name:      "an unknown algorithm among valid ones still refuses",
			requested: []string{ssh.KeyAlgoED25519, "rsa"},
			wantErr:   `Unknown host-key algorithm "rsa"`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, diags := parseKeyTypes(tc.requested)
			if tc.wantErr != "" {
				if !diags.HasError() {
					t.Fatalf("parseKeyTypes(%v) raised no error, want one containing %q",
						tc.requested, tc.wantErr)
				}
				text := diagText(diags.Errors())
				if !strings.Contains(text, tc.wantErr) {
					t.Errorf("errors = %q, want one containing %q", text, tc.wantErr)
				}
				// The message has to list what IS accepted, or an operator who
				// guessed the short alias has nothing to correct it to.
				if !strings.Contains(text, ssh.KeyAlgoED25519) {
					t.Errorf("the refusal does not say what is accepted: %q", text)
				}
				return
			}
			if diags.HasError() {
				t.Fatalf("parseKeyTypes(%v): %s", tc.requested, diagText(diags.Errors()))
			}
			if !slices.Equal(got, tc.want) {
				t.Errorf("parseKeyTypes(%v) = %v, want %v", tc.requested, got, tc.want)
			}
		})
	}
}

// The default must not contain `ssh-rsa`: it is RSA signed with SHA-1, which
// OpenSSH has disabled server-side since 8.8, so it would fail on most of a
// modern fleet and put an error line in every host's warning -- while
// `rsa-sha2-512` reaches the same RSA key.
func TestTheDefaultAlgorithmsAreSupportedAndExcludeSHA1RSA(t *testing.T) {
	for _, algo := range defaultKeyTypes {
		if !slices.Contains(supportedKeyTypes, algo) {
			t.Errorf("default algorithm %q is not in the supported set, so it would be refused", algo)
		}
	}
	if slices.Contains(defaultKeyTypes, ssh.KeyAlgoRSA) {
		t.Errorf("%q must not be a default: it is SHA-1 RSA, refused by default server-side since "+
			"OpenSSH 8.8", ssh.KeyAlgoRSA)
	}
	// One algorithm per key family, which is what makes the default a single
	// connection per family rather than several reaching the same key.
	if len(defaultKeyTypes) != 3 {
		t.Errorf("defaultKeyTypes = %v; the default is one algorithm per key family a present-day "+
			"sshd is likely to hold", defaultKeyTypes)
	}
}

func TestParseTimeout(t *testing.T) {
	for _, tc := range []struct {
		name    string
		seconds int64
		want    time.Duration
		wantErr string
	}{
		{name: "one second is the floor and is accepted", seconds: 1, want: time.Second},
		{name: "the default", seconds: defaultTimeoutSeconds, want: 5 * time.Second},
		{name: "a long timeout is allowed", seconds: 600, want: 600 * time.Second},
		// Zero would abandon every host before it answered, reporting a whole
		// fleet unreachable for a reason that is in the configuration.
		{name: "zero is refused", seconds: 0, wantErr: "must be at least 1"},
		{name: "a negative timeout is refused", seconds: -5, wantErr: "must be at least 1"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, diags := parseTimeout(tc.seconds)
			if tc.wantErr != "" {
				if !diags.HasError() {
					t.Fatalf("parseTimeout(%d) raised no error, want one containing %q",
						tc.seconds, tc.wantErr)
				}
				if !strings.Contains(diagText(diags.Errors()), tc.wantErr) {
					t.Errorf("errors = %q, want one containing %q", diagText(diags.Errors()), tc.wantErr)
				}
				return
			}
			if diags.HasError() {
				t.Fatalf("parseTimeout(%d): %s", tc.seconds, diagText(diags.Errors()))
			}
			if got != tc.want {
				t.Errorf("parseTimeout(%d) = %s, want %s", tc.seconds, got, tc.want)
			}
		})
	}
}
