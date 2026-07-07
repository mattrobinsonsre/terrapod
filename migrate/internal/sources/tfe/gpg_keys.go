package tfe

import (
	"context"
	"fmt"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

// GPGKeys lists the private registry's provider signing PUBLIC keys and
// translates them to ir.GPGKey. Only the public key is portable — the
// private key never leaves the operator — so this pre-registers the
// public keys and provider *versions* still re-publish via
// terrapod-publish (which owns the signature).
func (c *Client) GPGKeys(ctx context.Context) ([]ir.GPGKey, error) {
	list, err := c.API.GPGKeys.ListPrivate(ctx, tfe.GPGKeyListOptions{
		Namespaces: []string{c.OrgName},
	})
	if err != nil {
		return nil, fmt.Errorf("list gpg keys: %w", err)
	}
	var keys []ir.GPGKey
	for _, k := range list.Items {
		if key, ok := gpgKeyToIR(k); ok {
			keys = append(keys, key)
		}
	}
	return keys, nil
}

// gpgKeyToIR translates one go-tfe GPGKey. Pure — unit-testable. A key
// with no ASCII armor is skipped (nothing to register).
func gpgKeyToIR(k *tfe.GPGKey) (ir.GPGKey, bool) {
	if k == nil || k.AsciiArmor == "" {
		return ir.GPGKey{}, false
	}
	return ir.GPGKey{
		SourceID:   k.ID,
		ASCIIArmor: k.AsciiArmor,
		KeyID:      k.KeyID,
	}, true
}
