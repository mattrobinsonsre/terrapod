package ssh_host_keys

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"
)

// supportedKeyTypes are the host-key algorithms a configuration may ask for,
// named by the x/crypto constants rather than by their string values so that a
// rename upstream is a compile failure here rather than a configuration that
// quietly stops matching anything.
//
// It is hand-listed because x/crypto's own `supportedHostKeyAlgos` is
// unexported, so there is nothing to derive it from. Two omissions are
// deliberate rather than oversights:
//
//   - DSA. The library spells it `InsecureKeyAlgoDSA`, documents it as removed
//     from major implementations, and supports it only at insecure key sizes. A
//     scanner that can pin a 1024-bit DSA host key is not a convenience worth
//     the surface.
//   - The ML-DSA algorithms, and the `sk-*` FIDO ones. Neither appears in
//     x/crypto v0.58.0's supported host-key set, so asking for one would fail
//     the handshake with an error about the algorithm rather than about the
//     host — which reads as the host being broken.
var supportedKeyTypes = []string{
	ssh.KeyAlgoED25519,
	ssh.KeyAlgoECDSA256,
	ssh.KeyAlgoECDSA384,
	ssh.KeyAlgoECDSA521,
	ssh.KeyAlgoRSASHA256,
	ssh.KeyAlgoRSASHA512,
	ssh.KeyAlgoRSA,
}

// defaultKeyTypes is what a configuration naming none asks for: one current
// algorithm per key family a present-day sshd is likely to hold.
//
// `ssh-rsa` is absent on purpose. It is RSA signed with SHA-1, which OpenSSH
// has disabled server-side by default since 8.8, so asking for it by default
// would produce a failed attempt on most of a modern fleet and an error line in
// the diagnostic for every host. `rsa-sha2-512` reaches the same RSA host key.
var defaultKeyTypes = []string{ssh.KeyAlgoED25519, ssh.KeyAlgoECDSA256, ssh.KeyAlgoRSASHA512}

// clientVersion is what the target's log will record for this connection.
//
// The protocol requires the string to begin "SSH-2.0-", and the default the
// library supplies is a bare "SSH-2.0-Go". Naming ourselves instead is the
// difference between an operator finding an unexplained Go client in
// `auth.log` and finding the thing they configured.
const clientVersion = "SSH-2.0-terrapod_hostkeyscan"

// maxConcurrentScans bounds the fan-out. A fleet of hundreds must not be
// scanned serially -- that is minutes of plan time for a read that is mostly
// waiting on sockets -- and it must not be scanned all at once either, which
// would open hundreds of connections from one runner pod and look to anything
// watching the network like exactly what it is not.
const maxConcurrentScans = 16

// scanResult is what one host answered.
type scanResult struct {
	// keys are the host keys obtained, in the order the algorithms were asked
	// for, with duplicates removed.
	//
	// The dedupe is not belt-and-braces: `rsa-sha2-256` and `rsa-sha2-512` are
	// signature algorithms over the SAME `ssh-rsa` key -- x/crypto's own
	// comment on those constants says they "can't appear as a PublicKey.Type"
	// and that "the corresponding PublicKey.Type is KeyAlgoRSA" -- so asking
	// for both returns one key twice.
	keys []ssh.PublicKey
	// errs is one line per algorithm that produced nothing, carrying the
	// algorithm and the error verbatim.
	//
	// It is a list of strings rather than a single error because the
	// distinction an operator needs is in the text: "connection refused" and
	// "no common algorithm for host key" are both "this read got no key", and
	// only one of them means the host is down. Collapsing them into one
	// summary would throw away the half that says which.
	errs []string
}

// hostKeyFetcher dials one address and returns the host keys it presents for
// the requested algorithms.
//
// It is a function type so the network half is substitutable. Every test of the
// decision logic drives a fetcher of its own rather than a socket, and the one
// that drives a real socket drives a real in-process sshd, so nothing in this
// package's test suite depends on reaching a host that exists.
type hostKeyFetcher func(ctx context.Context, address string, keyTypes []string, timeout time.Duration) scanResult

// errCaptured aborts the handshake the instant the host key is in hand.
//
// It is not a failure. The host-key callback is the only place the key is
// offered, and returning nil would carry the connection on into
// authentication -- which this resource has no business attempting against
// somebody's fleet. `ssh-keyscan` aborts in the same place and for the same
// reason.
var errCaptured = errors.New("host key captured")

// dialForHostKeys is the production fetcher: one TCP connection per requested
// algorithm, exactly as `ssh-keyscan` makes one per `-t` entry.
//
// It has to be one connection per algorithm rather than one carrying the whole
// list. `HostKeyAlgorithms` is the set the client will ACCEPT, and the server
// picks its single most preferred match from it -- so asking for three and
// taking what comes back yields one key, not three, and which one is the
// server's choice rather than ours.
func dialForHostKeys(ctx context.Context, address string, keyTypes []string, timeout time.Duration) scanResult {
	var out scanResult
	seen := map[string]bool{}
	for _, algo := range keyTypes {
		key, err := oneHostKey(ctx, address, algo, timeout)
		if err != nil {
			out.errs = append(out.errs, fmt.Sprintf("%s: %v", algo, err))
			continue
		}
		// Keyed on the wire form, which is what makes two algorithms over one
		// RSA key collapse to a single entry.
		fingerprint := key.Type() + ":" + string(key.Marshal())
		if seen[fingerprint] {
			continue
		}
		seen[fingerprint] = true
		out.keys = append(out.keys, key)
	}
	return out
}

// oneHostKey opens one connection and returns the host key the server offers
// for a single algorithm.
//
// NOTHING IS SENT THAT IDENTIFIES A USER. The handshake order is version
// exchange, key exchange -- where the server presents its host key and a
// signature over the exchange hash -- and only then authentication. Aborting in
// the host-key callback happens before the username is ever written, so this
// leaves a connection in the target's log and no attempted login.
//
// AND THE KEY IS VERIFIED, not merely received. x/crypto checks the server's
// signature over the exchange hash against the offered key BEFORE calling the
// callback, so a key that arrives here is one the responder demonstrably holds
// the private half of. That does not make this anything other than
// trust-on-first-use -- it says nothing about whether the responder is the host
// you meant -- but it does rule out a passive observer forging an answer.
func oneHostKey(ctx context.Context, address, algo string, timeout time.Duration) (ssh.PublicKey, error) {
	d := net.Dialer{Timeout: timeout}
	conn, err := d.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, err
	}
	defer func() { _ = conn.Close() }()

	// The handshake itself is not context-aware, so the deadline is what bounds
	// it. Without one, a host that completes the TCP connection and then says
	// nothing holds the read open for as long as the kernel allows -- which is
	// the shape a firewall that accepts and drops produces, and it is the one a
	// plan can least afford to wait on.
	_ = conn.SetDeadline(time.Now().Add(timeout))

	var captured ssh.PublicKey
	cfg := &ssh.ClientConfig{
		ClientVersion:     clientVersion,
		HostKeyAlgorithms: []string{algo},
		Timeout:           timeout,
		HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
			captured = key
			return errCaptured
		},
	}
	_, _, _, err = ssh.NewClientConn(conn, address, cfg)
	if captured != nil {
		// The error is errCaptured, which is the success path.
		return captured, nil
	}
	if err == nil {
		return nil, errors.New("ssh: the handshake completed without offering a host key")
	}
	return nil, err
}

// scanAll scans every target through a bounded worker pool.
//
// The map is keyed on the target's configured entry rather than on its dial
// address, because that is the key every attribute this resource emits uses --
// so an operator reading `unreachable` and indexing into `entries` is using one
// vocabulary rather than two.
func scanAll(
	ctx context.Context,
	targets []aHost,
	keyTypes []string,
	timeout time.Duration,
	fetch hostKeyFetcher,
) map[string]scanResult {
	out := make(map[string]scanResult, len(targets))
	if len(targets) == 0 {
		return out
	}

	workers := min(maxConcurrentScans, len(targets))

	var mu sync.Mutex
	var wg sync.WaitGroup
	work := make(chan aHost)

	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for t := range work {
				res := fetch(ctx, t.address, keyTypes, timeout)
				mu.Lock()
				out[t.key] = res
				mu.Unlock()
			}
		}()
	}
	for _, t := range targets {
		work <- t
	}
	close(work)
	wg.Wait()

	return out
}
