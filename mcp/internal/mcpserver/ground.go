package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerGround adds the read-only "Ground" tools — the private-registry
// context an agent needs to write CORRECT config against *this* estate: which
// modules/providers exist here, and (the load-bearing one) a module version's
// input/output interface so the agent authors a valid `module` block instead of
// guessing variable names. All read-only; bounded by the caller's registry RBAC.
func registerGround(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_registry_module_list ────────────────────────────────
	type moduleListOut struct {
		Count   int                       `json:"count"`
		Modules []terrapod.RegistryModule `json:"modules"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_registry_module_list",
		Description: "List the private registry modules published on this instance (name, provider, VCS wiring, status). Use to discover what modules are available before authoring config.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, moduleListOut, error) {
		mods, err := c.ListRegistryModules(ctx)
		if err != nil {
			return errResult(err), moduleListOut{}, nil
		}
		return nil, moduleListOut{Count: len(mods), Modules: mods}, nil
	})

	// ── terrapod_registry_module_get ─────────────────────────────────
	type moduleGetIn struct {
		Name     string `json:"name" jsonschema:"the module name"`
		Provider string `json:"provider" jsonschema:"the module's provider (e.g. aws)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_registry_module_get",
		Description: "Get one private registry module by name + provider — its VCS source, status, owner, labels. To see a version's inputs/outputs, use terrapod_registry_module_interface.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in moduleGetIn) (*mcp.CallToolResult, *terrapod.RegistryModule, error) {
		if in.Name == "" || in.Provider == "" {
			return errText("name and provider are required"), nil, nil
		}
		mod, err := c.GetRegistryModule(ctx, in.Name, in.Provider)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, mod, nil
	})

	// ── terrapod_registry_module_interface ───────────────────────────
	type moduleInterfaceIn struct {
		Name     string `json:"name" jsonschema:"the module name"`
		Provider string `json:"provider" jsonschema:"the module's provider (e.g. aws)"`
		Version  string `json:"version" jsonschema:"the module version (e.g. 1.2.3)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_registry_module_interface",
		Description: "Get a module version's input variables and outputs — the exact surface to author a correct `module` block against it (variable names, types, whether required, defaults; and what it returns). " +
			"Prefer this over guessing a module's inputs. Returns 404 if interface extraction is disabled on this instance or the version doesn't exist. " +
			"If `interface-error` is present the module's files could not be parsed: empty or partial inputs then do not mean the module takes no variables.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in moduleInterfaceIn) (*mcp.CallToolResult, *terrapod.ModuleInterface, error) {
		if in.Name == "" || in.Provider == "" || in.Version == "" {
			return errText("name, provider and version are required"), nil, nil
		}
		iface, err := c.GetModuleInterface(ctx, in.Name, in.Provider, in.Version)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, iface, nil
	})

	// ── terrapod_catalog_item_interface ──────────────────────────────
	type catalogItemInterfaceIn struct {
		CatalogItemID string `json:"catalog_item_id" jsonschema:"the service-catalog item's id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_interface",
		Description: "Get a service-catalog item's module interface: the inputs and outputs of the module version the item resolves to (its version pin, or the latest uploaded version). " +
			"Use it to see what an instance of the item takes and what it will expose; the provision form users fill in is a curated subset of these inputs. " +
			"Needs catalog read on the item. The fields are null while the module has no uploaded version. " +
			"If `interface-error` is present the module's files could not be parsed, so empty inputs do not mean the item takes none.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in catalogItemInterfaceIn) (*mcp.CallToolResult, *terrapod.CatalogItemInterface, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), nil, nil
		}
		iface, err := c.GetCatalogItemInterface(ctx, in.CatalogItemID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, iface, nil
	})

	// ── terrapod_module_autodiscovery_rule_list ──────────────────────
	type moduleRuleListOut struct {
		Count int                                `json:"count"`
		Rules []terrapod.ModuleAutodiscoveryRule `json:"rules"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_module_autodiscovery_rule_list",
		Description: "List the module autodiscovery rules: each names a repository, an org or group, or a repository name pattern (`target-kind`: repository, namespace or pattern), " +
			"which directories count as modules (a glob pattern and ignore paths) and how they are named. A rule registers new module directories as they appear, and an org-wide rule also " +
			"registers the modules of repositories created after it. `last-error` says why a rule's last poll failed. Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, moduleRuleListOut, error) {
		rules, err := c.ListAllModuleAutodiscoveryRules(ctx)
		if err != nil {
			return errResult(err), moduleRuleListOut{}, nil
		}
		return nil, moduleRuleListOut{Count: len(rules), Rules: rules}, nil
	})

	// ── terrapod_module_autodiscovery_rule_preview ───────────────────
	type moduleRulePreviewIn struct {
		RuleID     string `json:"rule_id" jsonschema:"the rule id (modrule-...)"`
		Repository string `json:"repository,omitempty" jsonschema:"for an org-wide rule: read this one repository (a path such as org/terraform-aws-vpc, or its URL) live instead of the stored candidates"`
		PageNumber int    `json:"page_number,omitempty" jsonschema:"for an org-wide rule: which page of repositories (1-based; default 1)"`
		PageSize   int    `json:"page_size,omitempty" jsonschema:"for an org-wide rule: repositories per page (default the server's; at most 100)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_module_autodiscovery_rule_preview",
		Description: "What a module autodiscovery rule finds now: each module directory (the root and any submodules) with its `repository`, the name and provider it would be registered under, " +
			"the module already registered from it, and whether its name is taken. A single-repository rule reads its repository live. An org-wide rule is served from what the poller last found, " +
			"a page of repositories at a time (`repositories` gives each one's status; `pagination` the pages), or reads one `repository` live. Registers nothing. Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in moduleRulePreviewIn) (*mcp.CallToolResult, *terrapod.ModuleAutodiscoveryPreview, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), nil, nil
		}
		p, err := c.PreviewModuleAutodiscoveryRuleWithOptions(ctx, in.RuleID, terrapod.ModuleAutodiscoveryPreviewOptions{
			Repository: in.Repository, PageNumber: in.PageNumber, PageSize: in.PageSize,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, p, nil
	})

	// ── terrapod_module_autodiscovery_rule_repositories ──────────────
	type moduleRuleReposIn struct {
		RuleID     string `json:"rule_id" jsonschema:"the rule id (modrule-...)"`
		Status     string `json:"status,omitempty" jsonschema:"only repositories in this status: active, archived, empty, no-branch, out-of-scope, covered or error"`
		PageNumber int    `json:"page_number,omitempty" jsonschema:"which page (1-based; default 1)"`
		PageSize   int    `json:"page_size,omitempty" jsonschema:"repositories per page (default 50, at most 100)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_module_autodiscovery_rule_repositories",
		Description: "The repositories a module autodiscovery rule looks at, by path, each with its state: `status` (active; archived, kept but not scanned; empty; no-branch; " +
			"out-of-scope, left the rule's target with its modules kept; covered, named by a single-repository rule; or error, with `last-error`), `origin` (baseline: existed when the rule " +
			"took its baseline, so nothing registers until it is scanned; new: created afterwards, so its modules register automatically), the candidates its last poll found, the last " +
			"skips, and previous paths after a rename. Use it to see why a repository registered nothing. Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in moduleRuleReposIn) (*mcp.CallToolResult, *terrapod.ModuleAutodiscoveryRepositoryList, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), nil, nil
		}
		size := in.PageSize
		if size <= 0 {
			size = 50
		}
		list, err := c.ListModuleAutodiscoveryRuleRepositories(ctx, in.RuleID, terrapod.ModuleAutodiscoveryRepositoryListOptions{
			Status: in.Status, PageNumber: in.PageNumber, PageSize: size,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, list, nil
	})

	// ── terrapod_registry_provider_list ──────────────────────────────
	type providerListOut struct {
		Count     int                         `json:"count"`
		Providers []terrapod.RegistryProvider `json:"providers"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_registry_provider_list",
		Description: "List the private registry providers published on this instance (name, namespace, owner, labels). Use to discover which internal providers are available.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, providerListOut, error) {
		provs, err := c.ListRegistryProviders(ctx)
		if err != nil {
			return errResult(err), providerListOut{}, nil
		}
		return nil, providerListOut{Count: len(provs), Providers: provs}, nil
	})

	// ── terrapod_registry_provider_get ───────────────────────────────
	type providerGetIn struct {
		Name string `json:"name" jsonschema:"the provider name"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_registry_provider_get",
		Description: "Get one private registry provider by name — namespace, owner, labels.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in providerGetIn) (*mcp.CallToolResult, *terrapod.RegistryProvider, error) {
		if in.Name == "" {
			return errText("name is required"), nil, nil
		}
		prov, err := c.GetRegistryProvider(ctx, in.Name)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, prov, nil
	})
}
