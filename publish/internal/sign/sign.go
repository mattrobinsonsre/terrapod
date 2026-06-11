// Package sign produces the detached OpenPGP signature over SHA256SUMS that the
// Terrapod registry verifies against a publisher's registered GPG key. It uses
// the pure-Go ProtonMail/go-crypto OpenPGP implementation — no gpg binary.
package sign

import (
	"bytes"
	"errors"
	"strings"

	"github.com/ProtonMail/go-crypto/openpgp"
)

// LoadPrivateKey parses an ASCII-armored private key. If the key material is
// passphrase-protected, passphrase is used to unlock it (pass "" for an
// unprotected key). The returned entity is ready to sign.
func LoadPrivateKey(armored, passphrase string) (*openpgp.Entity, error) {
	keyring, err := openpgp.ReadArmoredKeyRing(strings.NewReader(armored))
	if err != nil {
		return nil, err
	}
	if len(keyring) == 0 {
		return nil, errors.New("no key found in armored private key")
	}
	entity := keyring[0]
	if entity.PrivateKey == nil {
		return nil, errors.New("armored block is a public key, not a private key")
	}
	if passphrase != "" {
		pw := []byte(passphrase)
		if entity.PrivateKey.Encrypted {
			if err := entity.PrivateKey.Decrypt(pw); err != nil {
				return nil, err
			}
		}
		for _, sk := range entity.Subkeys {
			if sk.PrivateKey != nil && sk.PrivateKey.Encrypted {
				_ = sk.PrivateKey.Decrypt(pw)
			}
		}
	}
	return entity, nil
}

// DetachSign produces a binary detached signature over data — exactly the
// format `gpg --detach-sign` emits and that terraform/tofu (and Terrapod's
// pgpy verifier) expect for SHA256SUMS.sig.
func DetachSign(entity *openpgp.Entity, data []byte) ([]byte, error) {
	var buf bytes.Buffer
	if err := openpgp.DetachSign(&buf, entity, bytes.NewReader(data), nil); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// KeyID returns the 16-hex-char long key ID of the entity's primary key,
// uppercased — the form Terrapod stores and matches signatures against.
func KeyID(entity *openpgp.Entity) string {
	return strings.ToUpper(entity.PrimaryKey.KeyIdString())
}
