package terrapod

import (
	"context"
	"encoding/json"
)

// EncryptionStatus reports application-layer encryption-at-rest health (#553).
// Decryptable is the headline durability signal — false means the platform
// cannot currently read its encrypted data back (investigate immediately).
type EncryptionStatus struct {
	Enabled       bool   `json:"enabled"`
	Provider      string `json:"provider"`
	ActiveVersion *int   `json:"active_version"`
	DEKVersions   []int  `json:"dek_versions"`
	CanaryOK      bool   `json:"canary_ok"`
	Decryptable   bool   `json:"decryptable"`
}

// GetEncryptionStatus fetches encryption-at-rest status. Requires platform admin.
func (c *Client) GetEncryptionStatus(ctx context.Context) (*EncryptionStatus, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/admin/encryption")
	if err != nil {
		return nil, err
	}
	var env struct {
		Data struct {
			Attributes EncryptionStatus `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, err
	}
	return &env.Data.Attributes, nil
}
