package terrapod

import (
	"context"
	"encoding/json"
)

// WarmPlatform is an os/arch target for cache pre-population.
type WarmPlatform struct {
	OS   string `json:"os"`
	Arch string `json:"arch"`
}

// WarmBinaryEntry requests pre-population of a terraform/tofu/terragrunt
// binary. An empty Platforms list lets the server fall back to its default
// warm platforms (linux/amd64 + linux/arm64).
type WarmBinaryEntry struct {
	Tool      string         `json:"tool,omitempty"`
	Version   string         `json:"version"`
	Platforms []WarmPlatform `json:"platforms,omitempty"`
}

// WarmProviderEntry requests pre-population of a provider. Source is the
// provider address "hostname/namespace/type". An empty Platforms list lets the
// server fall back to its configured provider_cache.platforms.
type WarmProviderEntry struct {
	Source    string         `json:"source"`
	Version   string         `json:"version"`
	Platforms []WarmPlatform `json:"platforms,omitempty"`
}

// BulkWarmRequest warms many binaries and/or provider platforms in one call.
type BulkWarmRequest struct {
	Binaries  []WarmBinaryEntry   `json:"binaries,omitempty"`
	Providers []WarmProviderEntry `json:"providers,omitempty"`
}

// WarmResult is the per-target outcome of a bulk warm.
type WarmResult struct {
	Kind  string `json:"kind"` // "binary" | "provider"
	Ref   string `json:"ref"`
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

// BulkWarmResponse aggregates a bulk warm run.
type BulkWarmResponse struct {
	Total     int          `json:"total"`
	Succeeded int          `json:"succeeded"`
	Failed    int          `json:"failed"`
	Results   []WarmResult `json:"results"`
}

// WarmCacheBulk pre-populates the binary and provider caches. It returns the
// per-target results even when some entries failed (the call only errors on a
// transport failure or a non-2xx response such as 400/403) — inspect
// Failed/Results for partial outcomes. Requires platform admin.
func (c *Client) WarmCacheBulk(ctx context.Context, req BulkWarmRequest) (*BulkWarmResponse, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	body, err := c.Post(ctx, "/api/terrapod/v1/admin/binary-cache/warm-bulk", payload)
	if err != nil {
		return nil, err
	}
	var out BulkWarmResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
