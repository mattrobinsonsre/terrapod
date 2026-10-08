package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerInventory adds the declared-inventory tools: the hosts a workspace
// declares through its own Terraform (#1968) and the inventory object those
// sources resolve into (#1967).
//
// # Why the limit preview is the tool that earns its place
//
// Auto-configure is deliberately broad — the user's decision, in their words:
// "People operating at scale might prefer the blast radius to the manual
// processes." A change to a playbook, a role or an inventory source can
// therefore reconfigure everything that source reaches, with no human asked.
// That is the intended behaviour, so **visibility is the control rather than
// prevention**, and an agent being able to answer "what would this target"
// before anything runs is the safety surface this whole group exists to give
// it. Ask the preview, show the host list, then act.
//
// # Reads only, and there is nothing to write
//
// No tool here declares or removes a host: a declared host is owned by the
// managing Terraform's `terrapod_inventory_item` resource, so an agent that
// wants one edits the configuration and lets the apply do it.
//
// Nor is there a refresh. Dynamic inventory was declined (#1970), so every
// source is static and `terrapod_inventory_resolved` resolves the rows to
// answer the call. There is no stale copy for a write tool to bring up to
// date, which is why the group is read-only in both senses: it changes
// nothing, and there is nothing it could usefully change.
func registerInventory(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_inventory_list ──────────────────────────────────────
	type inventoryItemListIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose declared hosts to list"`
	}
	type inventoryItemListOut struct {
		Count int                      `json:"count"`
		Items []terrapod.InventoryItem `json:"items"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_list",
		Description: "List the hosts a workspace DECLARES for ansible — each one's name, `address` (ansible_host), groups and host variables. " +
			"These are written by the managing Terraform's `terrapod_inventory_item` resource, so this is the workspace's own contribution to its inventory, not the whole inventory: " +
			"an inventory merges these with any git, UI-edited or external sources it also carries (see terrapod_workspace_inventories for the ordering, and terrapod_inventory_resolved for the merged result). " +
			"Pages through every host, so it is the full declared set rather than one page. " +
			"Empty for a workspace that declares none — most workspaces manage infrastructure and no hosts.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryItemListIn) (*mcp.CallToolResult, inventoryItemListOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), inventoryItemListOut{}, nil
		}
		items, err := c.ListAllInventoryItems(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), inventoryItemListOut{}, nil
		}
		// A nil slice marshals to `null`, which fails the tool's derived output
		// schema ("want array") and costs the agent the whole listing rather
		// than reporting an empty set — and an empty set is the common answer
		// here, not an edge case.
		if items == nil {
			items = []terrapod.InventoryItem{}
		}
		return nil, inventoryItemListOut{Count: len(items), Items: items}, nil
	})

	// ── terrapod_workspace_inventories ───────────────────────────────
	type workspaceInventoriesIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose inventories to list"`
	}
	type workspaceInventoriesOut struct {
		Count       int                  `json:"count"`
		Inventories []terrapod.Inventory `json:"inventories"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_inventories",
		Description: "List a workspace's ansible inventories and the ordered sources each one composes. " +
			"`position` is the `-i` ordering, so a HIGHER position wins a conflicting host variable (hosts and group memberships union; the conflict is per variable, so a non-conflicting variable from an earlier source survives). " +
			"Every source is static, so terrapod_inventory_resolved resolves any of these to a current answer; there is no source here that needs a runner to parse. " +
			"Empty for a workspace that has never declared a host — an inventory is created when something uses one.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceInventoriesIn) (*mcp.CallToolResult, workspaceInventoriesOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), workspaceInventoriesOut{}, nil
		}
		list, err := c.ListInventories(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), workspaceInventoriesOut{}, nil
		}
		if list == nil {
			list = []terrapod.Inventory{}
		}
		return nil, workspaceInventoriesOut{Count: len(list), Inventories: list}, nil
	})

	// ── terrapod_inventory_resolved ──────────────────────────────────
	type inventoryResolvedIn struct {
		InventoryID string `json:"inventory_id" jsonschema:"the inventory id, from terrapod_workspace_inventories"`
		// Off by default: see the tool description for why the exhaustive
		// `hosts` map is the one to reason over.
		IncludeAnsibleShape bool `json:"include_ansible_shape,omitempty" jsonschema:"also return the same resolution in the shape 'ansible-inventory --list' produces. Off by default; reason over 'hosts' instead, and ask for this only to hand the literal document to something that wants it"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_resolved",
		Description: "Read what an inventory resolves to: every host with its merged variables, every group's membership, and how many of each. " +
			"This is the target set a configure would run against, so it is what to show a user before anything runs. " +
			"`hosts` is EXHAUSTIVE — it includes a host with no variables, deliberately unlike ansible's own `_meta.hostvars`, which omits one entirely; enumerating a host set from ansible's shape loses hosts silently, which is why `hosts` is the map to reason over and the ansible-shaped rendering is off unless asked for. " +
			"It is LIVE and there is nothing to refresh. Dynamic inventory was declined, so every source is static and Terrapod resolves the rows to answer this call — the hosts you read are the hosts as they are, not as of some earlier moment. There is deliberately no timestamp and no freshness field: a reader has nothing to reason about, and no write tool is needed to bring this up to date.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryResolvedIn) (*mcp.CallToolResult, *terrapod.ResolvedInventory, error) {
		if in.InventoryID == "" {
			return errText("inventory_id is required"), nil, nil
		}
		resolved, err := c.GetResolvedInventory(ctx, in.InventoryID)
		if err != nil {
			return errResult(err), nil, nil
		}
		if !in.IncludeAnsibleShape {
			resolved.AnsibleInventory = nil
		}
		return nil, resolved, nil
	})

	// ── terrapod_inventory_limit_preview ─────────────────────────────
	type inventoryLimitPreviewIn struct {
		InventoryID string `json:"inventory_id" jsonschema:"the inventory id, from terrapod_workspace_inventories"`
		Limit       string `json:"limit" jsonschema:"the ansible --limit pattern to expand: host names, group names, 'all' or '*', comma- or colon-separated terms, '!' to exclude and '&' to intersect. Pass 'all' explicitly to see every host; an empty pattern is refused so that the broadest possible target set is always something you asked for"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_limit_preview",
		Description: "Answer which hosts an ansible `--limit` pattern would select, BEFORE anything runs. Show the result to the user when a configure's blast radius matters. " +
			"Auto-configure is deliberately broad (operators at scale prefer the blast radius to the manual process), so visibility is the control rather than prevention, and this is the tool that provides it. " +
			"Two honesty conditions on the answer. It is ADVISORY: the authoritative expansion is `ansible-inventory --list --limit` taken in the runner at the start of the configure, and this covers the forms an operator writes rather than reimplementing ansible's pattern language — a `~regex` term is REFUSED rather than matched, because an empty target set for a pattern ansible would have expanded is the wrong answer dressed as an answer. " +
			"It expands against a LIVE resolution, so it reflects a host declared a moment ago; compare `host-count` against `of-host-count` (the hosts it expanded against) to see how much of the inventory the pattern selects.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryLimitPreviewIn) (*mcp.CallToolResult, *terrapod.InventoryLimitPreview, error) {
		if in.InventoryID == "" {
			return errText("inventory_id is required"), nil, nil
		}
		// An empty pattern is a valid ansible limit meaning "every host", and
		// the API expands it that way. Refusing it here is deliberate: an agent
		// that omits the field by accident would otherwise be told the whole
		// inventory is the target set, which is the one wrong answer that reads
		// as a successful narrow. `all` says the same thing on purpose.
		if in.Limit == "" {
			return errText("limit is required; pass 'all' to preview every host in the inventory"), nil, nil
		}
		preview, err := c.PreviewInventoryLimit(ctx, in.InventoryID, in.Limit)
		if err != nil {
			return errResult(err), nil, nil
		}
		// A pattern that matches nothing is a real and important answer, so it
		// must not arrive as `null` and fail the derived output schema.
		if preview.Hosts == nil {
			preview.Hosts = []string{}
		}
		return nil, preview, nil
	})
}
