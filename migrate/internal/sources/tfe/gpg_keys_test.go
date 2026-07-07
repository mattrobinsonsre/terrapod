package tfe

import (
	"testing"

	"github.com/hashicorp/go-tfe"
)

func TestGPGKeyToIR_Translation(t *testing.T) {
	k := &tfe.GPGKey{
		ID:         "gpgkey-1",
		KeyID:      "ABC123DEF456",
		AsciiArmor: "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END PGP PUBLIC KEY BLOCK-----",
	}
	got, ok := gpgKeyToIR(k)
	if !ok {
		t.Fatal("expected ok=true for a populated key")
	}
	if got.SourceID != "gpgkey-1" || got.KeyID != "ABC123DEF456" || got.ASCIIArmor != k.AsciiArmor {
		t.Errorf("translation: %+v", got)
	}
}

func TestGPGKeyToIR_SkipsEmptyArmor(t *testing.T) {
	// A key with no ASCII armor has nothing to register — skip it.
	if _, ok := gpgKeyToIR(&tfe.GPGKey{ID: "gpgkey-2", KeyID: "X"}); ok {
		t.Error("key with empty ascii armor should be skipped")
	}
	if _, ok := gpgKeyToIR(nil); ok {
		t.Error("nil key should be skipped")
	}
}
