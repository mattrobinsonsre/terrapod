package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// Module discovery (#1584): the modules in a repository, proposed for the
// registry. A scan registers nothing — pick candidates and create each with
// CreateRegistryModule, setting Subdirectory for a submodule.

// RegistryModuleDiscovery is one repository's proposed modules.
type RegistryModuleDiscovery struct {
	VCSRepoURL string                    `json:"vcs-repo-url"`
	VCSBranch  string                    `json:"vcs-branch"`
	Candidates []RegistryModuleCandidate `json:"candidates"`
}

// RegistryModuleCandidate is one directory holding Terraform files. The
// repository root has an empty Subdirectory.
type RegistryModuleCandidate struct {
	Subdirectory      string `json:"subdirectory"`
	SuggestedName     string `json:"suggested-name"`
	SuggestedProvider string `json:"suggested-provider"`
	// RegisteredAs is the module already registered for this directory of
	// the repository, or nil.
	RegisteredAs *RegisteredModuleRef `json:"registered-as,omitempty"`
}

// RegisteredModuleRef names an existing registry module.
type RegisteredModuleRef struct {
	Name     string `json:"name"`
	Provider string `json:"provider"`
}

// DiscoverRegistryModulesRequest selects the repository to scan. VCSBranch
// defaults to the repository's default branch.
type DiscoverRegistryModulesRequest struct {
	VCSConnectionID string
	VCSRepoURL      string
	VCSBranch       string
}

// DiscoverRegistryModules proposes the modules in a repository: every
// directory holding Terraform files, with a suggested name and provider, and
// whichever module already registers it. Platform admin only.
func (c *Client) DiscoverRegistryModules(ctx context.Context, req DiscoverRegistryModulesRequest) (*RegistryModuleDiscovery, error) {
	attrs := map[string]any{
		"vcs-connection-id": req.VCSConnectionID,
		"vcs-repo-url":      req.VCSRepoURL,
	}
	if req.VCSBranch != "" {
		attrs["vcs-branch"] = req.VCSBranch
	}
	body, err := MarshalResource("registry-module-discoveries", attrs, nil)
	if err != nil {
		return nil, fmt.Errorf("marshal registry-module discovery: %w", err)
	}
	data, err := c.Post(ctx, "/api/terrapod/v1/registry-modules/discover", body)
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse registry-module discovery: %w", err)
	}
	d := &RegistryModuleDiscovery{
		VCSRepoURL: GetStringAttr(res, "vcs-repo-url"),
		VCSBranch:  GetStringAttr(res, "vcs-branch"),
	}
	if raw, ok := res.Attributes["candidates"]; ok && len(raw) > 0 && string(raw) != "null" {
		if err := json.Unmarshal(raw, &d.Candidates); err != nil {
			return nil, fmt.Errorf("parse registry-module discovery candidates: %w", err)
		}
	}
	return d, nil
}
