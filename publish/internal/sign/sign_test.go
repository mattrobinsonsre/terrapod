package sign

import (
	"bytes"
	"testing"

	"github.com/ProtonMail/go-crypto/openpgp"
)

func testEntity(t *testing.T) *openpgp.Entity {
	t.Helper()
	e, err := openpgp.NewEntity("awsmai test", "", "security@example.test", nil)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func TestDetachSignVerifies(t *testing.T) {
	e := testEntity(t)
	data := []byte("abc123  terraform-provider-awsmai_1.0.0_linux_arm64.zip\n")
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
