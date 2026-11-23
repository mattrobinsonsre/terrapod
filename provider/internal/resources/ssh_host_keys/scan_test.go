package ssh_host_keys

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"errors"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

// The network half is driven against a REAL in-process sshd rather than a
// stub, because every claim this package makes about the handshake is a claim
// about x/crypto's behaviour and not about ours: that aborting in the host-key
// callback yields the key, that `rsa-sha2-512` produces a key whose type is
// `ssh-rsa`, that asking for an algorithm the server has no key for fails with
// an error naming the algorithm rather than hanging. A stub would assert my
// reading of the library, which is the thing most likely to be wrong.
//
// Nothing here reaches a host that exists; the listener is on 127.0.0.1 with a
// port the kernel picks.

// startSSHServer serves the given host keys on a loopback port and returns its
// address.
//
// The client aborts the handshake in its host-key callback, so NewServerConn
// returns an error on every successful scan. That is the success path and the
// error is deliberately ignored.
func startSSHServer(t *testing.T, signers ...ssh.Signer) string {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	cfg := &ssh.ServerConfig{
		// Authentication is never reached -- the client leaves before the
		// username is sent -- but a config with no auth method at all is
		// rejected by the library, so one that always refuses is the honest
		// placeholder.
		PasswordCallback: func(ssh.ConnMetadata, []byte) (*ssh.Permissions, error) {
			return nil, errors.New("this fixture authenticates nobody")
		},
	}
	for _, s := range signers {
		cfg.AddHostKey(s)
	}

	accepted := make(chan struct{})
	go func() {
		defer close(accepted)
		for {
			conn, acceptErr := ln.Accept()
			if acceptErr != nil {
				return
			}
			go func() {
				defer func() { _ = conn.Close() }()
				sc, chans, reqs, handshakeErr := ssh.NewServerConn(conn, cfg)
				if handshakeErr != nil {
					return
				}
				go ssh.DiscardRequests(reqs)
				for nc := range chans {
					_ = nc.Reject(ssh.UnknownChannelType, "this fixture serves no channels")
				}
				_ = sc.Close()
			}()
		}
	}()
	t.Cleanup(func() {
		_ = ln.Close()
		<-accepted
	})

	return ln.Addr().String()
}

func ed25519Signer(t *testing.T) ssh.Signer {
	t.Helper()
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("ed25519: %v", err)
	}
	s, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatalf("signer: %v", err)
	}
	return s
}

func ecdsaSigner(t *testing.T) ssh.Signer {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("ecdsa: %v", err)
	}
	s, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatalf("signer: %v", err)
	}
	return s
}

// rsaSigner is generated once for the whole package. 2048-bit RSA generation is
// the one slow thing in this file, and three tests need the same key.
var rsaSigner = sync.OnceValues(func() (ssh.Signer, error) {
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(priv)
})

func rsaHostSigner(t *testing.T) ssh.Signer {
	t.Helper()
	s, err := rsaSigner()
	if err != nil {
		t.Fatalf("rsa: %v", err)
	}
	return s
}

const probeTimeout = 5 * time.Second

func TestAScanReturnsTheHostKeyTheServerHolds(t *testing.T) {
	signer := ed25519Signer(t)
	addr := startSSHServer(t, signer)

	res := dialForHostKeys(context.Background(), addr, []string{ssh.KeyAlgoED25519}, probeTimeout)
	if len(res.errs) > 0 {
		t.Fatalf("unexpected errors: %v", res.errs)
	}
	if len(res.keys) != 1 {
		t.Fatalf("got %d keys, want 1", len(res.keys))
	}
	if got, want := res.keys[0].Type(), ssh.KeyAlgoED25519; got != want {
		t.Errorf("key type = %q, want %q", got, want)
	}
	if !bytesEqual(res.keys[0].Marshal(), signer.PublicKey().Marshal()) {
		t.Error("the scanned key is not the one the server holds")
	}
}

// The claim this pins is x/crypto's own, from the comment on its constants:
// "KeyAlgoRSASHA256 and KeyAlgoRSASHA512 are only public key algorithms, not
// public key formats, so they can't appear as a PublicKey.Type. The
// corresponding PublicKey.Type is KeyAlgoRSA."
//
// The whole comparison in reconcile is keyed on PublicKey.Type(), so if asking
// for `rsa-sha2-512` produced a key typed `rsa-sha2-512` the pin for an RSA
// host would never match itself and every read would report it changed.
func TestAnRSAKeyIsTypedSSHRSAHoweverItWasAskedFor(t *testing.T) {
	addr := startSSHServer(t, rsaHostSigner(t))

	for _, algo := range []string{ssh.KeyAlgoRSASHA512, ssh.KeyAlgoRSASHA256} {
		t.Run(algo, func(t *testing.T) {
			res := dialForHostKeys(context.Background(), addr, []string{algo}, probeTimeout)
			if len(res.errs) > 0 {
				t.Fatalf("unexpected errors: %v", res.errs)
			}
			if len(res.keys) != 1 {
				t.Fatalf("got %d keys, want 1", len(res.keys))
			}
			if got := res.keys[0].Type(); got != ssh.KeyAlgoRSA {
				t.Errorf("key type = %q, want %q", got, ssh.KeyAlgoRSA)
			}
		})
	}
}

// Asking for both RSA signature algorithms must yield ONE entry, or an RSA host
// would be pinned twice and the known_hosts body would carry a duplicate line.
func TestTheTwoRSAAlgorithmsCollapseToOneEntry(t *testing.T) {
	addr := startSSHServer(t, rsaHostSigner(t))

	res := dialForHostKeys(context.Background(), addr,
		[]string{ssh.KeyAlgoRSASHA512, ssh.KeyAlgoRSASHA256}, probeTimeout)
	if len(res.errs) > 0 {
		t.Fatalf("unexpected errors: %v", res.errs)
	}
	if len(res.keys) != 1 {
		t.Fatalf("got %d keys, want 1 -- the same RSA key reached by two signature algorithms", len(res.keys))
	}
}

func TestEveryRequestedAlgorithmIsItsOwnConnection(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t), ecdsaSigner(t))

	res := dialForHostKeys(context.Background(), addr,
		[]string{ssh.KeyAlgoED25519, ssh.KeyAlgoECDSA256}, probeTimeout)
	if len(res.errs) > 0 {
		t.Fatalf("unexpected errors: %v", res.errs)
	}
	// Both, and in the order asked for. One connection carrying both
	// algorithms would have returned whichever the server preferred, and only
	// that one.
	if len(res.keys) != 2 {
		t.Fatalf("got %d keys, want 2", len(res.keys))
	}
	if res.keys[0].Type() != ssh.KeyAlgoED25519 || res.keys[1].Type() != ssh.KeyAlgoECDSA256 {
		t.Errorf("key order = %q, %q; want the order key_types asked for",
			res.keys[0].Type(), res.keys[1].Type())
	}
}

// A host that is up and holds none of the requested key types must produce an
// error that SAYS so, because that and "the host is down" are the two cases
// `unreachable` deliberately covers together and the text is what tells them
// apart.
func TestAHostHoldingNoneOfTheRequestedTypesReportsTheAlgorithm(t *testing.T) {
	addr := startSSHServer(t, ed25519Signer(t))

	res := dialForHostKeys(context.Background(), addr, []string{ssh.KeyAlgoECDSA384}, probeTimeout)
	if len(res.keys) != 0 {
		t.Fatalf("got %d keys, want none", len(res.keys))
	}
	if len(res.errs) != 1 {
		t.Fatalf("got %d errors, want 1: %v", len(res.errs), res.errs)
	}
	if !strings.Contains(res.errs[0], ssh.KeyAlgoECDSA384) {
		t.Errorf("the error does not name the algorithm asked for: %q", res.errs[0])
	}
	if !strings.Contains(res.errs[0], "algorithm") {
		t.Errorf("the error should say the algorithms did not match, got %q", res.errs[0])
	}
}

func TestAClosedPortIsAnErrorAndNotAHang(t *testing.T) {
	// A listener opened and immediately closed leaves a port nothing is on.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	start := time.Now()
	res := dialForHostKeys(context.Background(), addr, []string{ssh.KeyAlgoED25519}, 2*time.Second)
	if len(res.keys) != 0 {
		t.Fatalf("got %d keys from a closed port", len(res.keys))
	}
	if len(res.errs) != 1 {
		t.Fatalf("got %d errors, want 1: %v", len(res.errs), res.errs)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("a refused connection took %s; the timeout is not bounding the attempt", elapsed)
	}
}

// The line's host field is what ssh matches on, so the bracket form for a
// non-default port is not cosmetic: `web1:2222 ssh-ed25519 …` would be read as
// a host literally called "web1:2222".
func TestANonDefaultPortIsWrittenInTheBracketForm(t *testing.T) {
	signer := ed25519Signer(t)
	addr := startSSHServer(t, signer)

	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("splitting the fixture's address: %v", err)
	}
	if port == "22" {
		t.Skip("the kernel handed out port 22, which this test cannot distinguish")
	}

	line := strings.TrimRight(knownhosts.Line([]string{addr}, signer.PublicKey()), "\n")
	want := "[" + host + "]:" + port + " "
	if !strings.HasPrefix(line, want) {
		t.Errorf("line = %q, want it to start %q", line, want)
	}

	// And the default port is written bare, which is the other half of the
	// same rule.
	bare := knownhosts.Line([]string{net.JoinHostPort("web1.example", "22")}, signer.PublicKey())
	if !strings.HasPrefix(bare, "web1.example ") {
		t.Errorf("port 22 should be written bare, got %q", bare)
	}
}

// Nothing identifying a user reaches the host: the handshake is version
// exchange, key exchange, THEN authentication, and this aborts in the host-key
// callback -- which fires during the key exchange. The fixture's auth callback
// records whether it was ever consulted.
func TestAScanNeverAttemptsAuthentication(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = ln.Close() }()

	var mu sync.Mutex
	var attempts []string
	cfg := &ssh.ServerConfig{
		// AuthLogCallback and not PasswordCallback. A client with no auth
		// methods configured never reaches password auth, but it DOES send a
		// "none" request carrying the username -- which is the thing this test
		// is about. Watching PasswordCallback alone made this pass with the
		// handshake abort removed, which a mutation found and reading did not.
		AuthLogCallback: func(c ssh.ConnMetadata, method string, _ error) {
			mu.Lock()
			attempts = append(attempts, method+" as "+c.User())
			mu.Unlock()
		},
		PasswordCallback: func(ssh.ConnMetadata, []byte) (*ssh.Permissions, error) {
			return nil, errors.New("refused")
		},
	}
	cfg.AddHostKey(ed25519Signer(t))

	served := make(chan struct{})
	go func() {
		defer close(served)
		conn, acceptErr := ln.Accept()
		if acceptErr != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		_, _, _, _ = ssh.NewServerConn(conn, cfg)
	}()

	res := dialForHostKeys(context.Background(), ln.Addr().String(),
		[]string{ssh.KeyAlgoED25519}, probeTimeout)
	if len(res.keys) != 1 {
		t.Fatalf("the scan did not get a key: %v", res.errs)
	}
	<-served

	mu.Lock()
	defer mu.Unlock()
	if len(attempts) > 0 {
		t.Errorf("authentication was attempted (%v); a scan must leave before the username is sent", attempts)
	}
}

func TestTheScanIdentifiesItselfInTheClientVersion(t *testing.T) {
	// The protocol requires the string to start "SSH-2.0-", and a host's log is
	// where an operator will go looking for what connected.
	if !strings.HasPrefix(clientVersion, "SSH-2.0-") {
		t.Fatalf("clientVersion = %q, which is not a legal SSH identification string", clientVersion)
	}
	if !strings.Contains(clientVersion, "terrapod") {
		t.Errorf("clientVersion = %q; it should name the thing doing the scanning", clientVersion)
	}
}

func TestScanAllVisitsEveryTargetExactlyOnce(t *testing.T) {
	// More targets than workers, so the pool genuinely recycles.
	var mu sync.Mutex
	counts := map[string]int{}
	fetch := func(_ context.Context, address string, _ []string, _ time.Duration) scanResult {
		mu.Lock()
		counts[address]++
		mu.Unlock()
		return scanResult{}
	}

	var targets []aHost
	for i := range maxConcurrentScans * 3 {
		key := "h" + string(rune('a'+i%26)) + string(rune('0'+i/26))
		targets = append(targets, aHost{key: key, address: key + ":22"})
	}

	out := scanAll(context.Background(), targets, []string{ssh.KeyAlgoED25519}, time.Second, fetch)
	if len(out) != len(targets) {
		t.Fatalf("scanned %d of %d targets", len(out), len(targets))
	}
	mu.Lock()
	defer mu.Unlock()
	for _, tgt := range targets {
		if counts[tgt.address] != 1 {
			t.Errorf("%s visited %d times, want 1", tgt.address, counts[tgt.address])
		}
	}
}

func TestScanAllOnNoTargetsDoesNothing(t *testing.T) {
	called := false
	fetch := func(context.Context, string, []string, time.Duration) scanResult {
		called = true
		return scanResult{}
	}
	out := scanAll(context.Background(), nil, nil, time.Second, fetch)
	if len(out) != 0 || called {
		t.Errorf("an empty host set must open no connections (called=%v, results=%d)", called, len(out))
	}
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
