package sign

import (
	"bytes"
	"strings"
	"testing"

	"github.com/ProtonMail/go-crypto/openpgp"
	"github.com/ProtonMail/go-crypto/openpgp/armor"
)

func testEntity(t *testing.T) *openpgp.Entity {
	t.Helper()
	e, err := openpgp.NewEntity("example test", "", "security@example.test", nil)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

// armorPrivate serialises an entity as an ASCII-armored private key block — the
// form LoadPrivateKey parses (what a `--signing-key KEY.asc` file contains).
func armorPrivate(t *testing.T, e *openpgp.Entity) string {
	t.Helper()
	var buf bytes.Buffer
	w, err := armor.Encode(&buf, openpgp.PrivateKeyType, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := e.SerializePrivate(w, nil); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.String()
}

// armorPublic serialises only the public half — LoadPrivateKey must reject it.
func armorPublic(t *testing.T, e *openpgp.Entity) string {
	t.Helper()
	var buf bytes.Buffer
	w, err := armor.Encode(&buf, openpgp.PublicKeyType, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := e.Serialize(w); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.String()
}

func TestLoadPrivateKeyRoundTrip(t *testing.T) {
	e := testEntity(t)
	loaded, err := LoadPrivateKey(armorPrivate(t, e), "")
	if err != nil {
		t.Fatal(err)
	}
	if loaded.PrivateKey == nil {
		t.Fatal("loaded entity has no private key")
	}
	if KeyID(loaded) != KeyID(e) {
		t.Errorf("key id mismatch: loaded %q, want %q", KeyID(loaded), KeyID(e))
	}
	// A key loaded from armor must be able to sign, and the signature must
	// verify against the same entity — proves the round-trip preserved usable
	// key material.
	data := []byte("abc123  provider_1.0.0_linux_amd64.zip\n")
	sig, err := DetachSign(loaded, data)
	if err != nil {
		t.Fatalf("sign with loaded key: %v", err)
	}
	if _, err := openpgp.CheckDetachedSignature(
		openpgp.EntityList{loaded}, bytes.NewReader(data), bytes.NewReader(sig), nil); err != nil {
		t.Fatalf("signature from loaded key did not verify: %v", err)
	}
}

func TestLoadPrivateKeyRejectsPublicKey(t *testing.T) {
	e := testEntity(t)
	_, err := LoadPrivateKey(armorPublic(t, e), "")
	if err == nil {
		t.Fatal("expected error loading a public key as private")
	}
	if !strings.Contains(err.Error(), "public key") {
		t.Errorf("error = %q, want it to mention 'public key'", err.Error())
	}
}

// Note: the passphrase-decrypt branch of LoadPrivateKey (an encrypted signing
// key) is not exercised here — go-crypto's SerializePrivate can't emit an
// encrypted armored block in-memory (it re-signs the identity, which needs the
// unlocked key), so covering it would require a committed pre-encrypted .asc
// fixture. Tracked as a follow-up rather than an in-memory quick win.

func TestLoadPrivateKeyRejectsGarbage(t *testing.T) {
	// Deliberately-malformed armored input: the PGP markers wrap the literal
	// text "not base64" — there is NO key material here. It exists only to
	// prove LoadPrivateKey rejects bad armor. The secret scanner matches the
	// marker text, not a real key (every real test key is generated at runtime
	// via openpgp.NewEntity), so this is a false positive.
	// Full check-id (registry rules carry a doubled leaf; the single-id form did
	// not match, so the suppression never took — GitHub code-scanning alert).
	const malformedArmor = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nnot base64\n-----END PGP PRIVATE KEY BLOCK-----\n" // nosemgrep: generic.secrets.security.detected-pgp-private-key-block.detected-pgp-private-key-block
	if _, err := LoadPrivateKey(malformedArmor, ""); err == nil {
		t.Fatal("expected error on malformed armor")
	}
	if _, err := LoadPrivateKey("not armored at all", ""); err == nil {
		t.Fatal("expected error on non-armored input")
	}
}

func TestDetachSignVerifies(t *testing.T) {
	e := testEntity(t)
	data := []byte("abc123  terraform-provider-example_1.0.0_linux_arm64.zip\n")
	sig, err := DetachSign(e, data)
	if err != nil {
		t.Fatal(err)
	}
	if len(sig) == 0 {
		t.Fatal("empty signature")
	}
	// Round-trip: the signature must verify against the entity over the bytes.
	signer, err := openpgp.CheckDetachedSignature(
		openpgp.EntityList{e}, bytes.NewReader(data), bytes.NewReader(sig), nil)
	if err != nil {
		t.Fatalf("signature did not verify: %v", err)
	}
	if signer.PrimaryKey.KeyId != e.PrimaryKey.KeyId {
		t.Errorf("verified by unexpected key")
	}
}

func TestDetachSignRejectsTamper(t *testing.T) {
	e := testEntity(t)
	sig, _ := DetachSign(e, []byte("original"))
	_, err := openpgp.CheckDetachedSignature(
		openpgp.EntityList{e}, bytes.NewReader([]byte("tampered")), bytes.NewReader(sig), nil)
	if err == nil {
		t.Fatal("expected verification failure on tampered data")
	}
}

func TestKeyIDFormat(t *testing.T) {
	e := testEntity(t)
	id := KeyID(e)
	if len(id) != 16 {
		t.Errorf("key id = %q, want 16 hex chars", id)
	}
	if id != bytesToUpper(id) {
		t.Errorf("key id not uppercased: %q", id)
	}
}

func bytesToUpper(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'a' && c <= 'f' {
			b[i] = c - 32
		}
	}
	return string(b)
}
