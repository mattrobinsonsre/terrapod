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
			"Prefer this over guessing a module's inputs. Returns 404 if interface extraction is disabled on this instance or the version doesn't exist.",
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
