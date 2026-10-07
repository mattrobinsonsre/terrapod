package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerInventory adds the declared-inventory tools: the hosts a workspace
// declares through its own Terraform (#1968), the inventory object those
// sources resolve into, and the snapshots a configure targets against (#1967).
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
// # Reads only, and deliberately
//
// There is no tool here that declares or removes a host. A declared host is
// owned by the managing Terraform's `terrapod_inventory_item` resource, so one
// written straight through the API is in no configuration's state: nothing
// prunes it (the server prunes snapshot history, never items), so it persists
// in the target set of every configure reading that inventory as an orphan no
// code owns, and it can collide with the name a later apply wants. An agent
// that wants a host declared edits the configuration and lets the apply do it.
// The one write here is a snapshot refresh, which is a record rather than a
// declaration and is therefore safe to drive directly.
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
			"`api-resolvable` is the field to read before anything else: false means a source needs ansible to parse, which only a runner has, so Terrapod cannot resolve that inventory itself and its newest snapshot may be older than the declared hosts. " +
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
		Description: "Read what an inventory currently resolves to: every host with its merged variables, every group's membership, how many of each, when the snapshot was taken (`taken-at`) and what produced it (`produced-by`: `api` or `runner`). " +
			"This is the target set a configure would run against, so it is what to show a user before anything runs. " +
			"`hosts` is EXHAUSTIVE — it includes a host with no variables, deliberately unlike ansible's own `_meta.hostvars`, which omits one entirely; enumerating a host set from ansible's shape loses hosts silently, which is why `hosts` is the map to reason over and the ansible-shaped rendering is off unless asked for. " +
			"Serves the newest snapshot, and takes the first one itself when there is none and every source is one Terrapod owns. " +
			"When a source needs ansible and no snapshot exists yet it answers a CONFLICT naming the offending source kinds, rather than resolving the rest: a partial resolution is a target set that is silently too small. Relay that message — it says a configure or a runner-side resolve has to produce the first snapshot, which is the actionable part.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryResolvedIn) (*mcp.CallToolResult, *terrapod.InventoryVersion, error) {
		if in.InventoryID == "" {
			return errText("inventory_id is required"), nil, nil
		}
		version, err := c.GetResolvedInventory(ctx, in.InventoryID)
		if err != nil {
			return errResult(err), nil, nil
		}
		if !in.IncludeAnsibleShape {
			version.AnsibleInventory = nil
		}
		return nil, version, nil
	})

	// ── terrapod_inventory_versions ──────────────────────────────────
	type inventoryVersionsIn struct {
		InventoryID string `json:"inventory_id" jsonschema:"the inventory id, from terrapod_workspace_inventories"`
	}
	type inventoryVersionsOut struct {
		Count    int                         `json:"count"`
		Versions []terrapod.InventoryVersion `json:"versions"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_versions",
		Description: "List an inventory's snapshot history, newest first — each one's host and group counts, `taken-at`, and `produced-by` (`api` or `runner`) with the ref that produced it. " +
			"Use it to answer how fresh the current target set is and whether a runner has ever resolved this inventory: an inventory whose snapshots are all `api`-produced has never been resolved by ansible itself. " +
			"Contents are omitted here; read terrapod_inventory_resolved for the hosts and groups of the current one.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryVersionsIn) (*mcp.CallToolResult, inventoryVersionsOut, error) {
		if in.InventoryID == "" {
			return errText("inventory_id is required"), inventoryVersionsOut{}, nil
		}
		versions, err := c.ListInventoryVersions(ctx, in.InventoryID)
		if err != nil {
			return errResult(err), inventoryVersionsOut{}, nil
		}
		if versions == nil {
			versions = []terrapod.InventoryVersion{}
		}
		return nil, inventoryVersionsOut{Count: len(versions), Versions: versions}, nil
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
			"And it is only as FRESH as the last snapshot, not live: read `taken-at`, and compare `host-count` against `of-host-count` (the hosts in that snapshot) to see how much of the inventory the pattern selects. " +
			"An inventory with no snapshot yet has nothing to limit against and answers a conflict; call terrapod_inventory_resolved first.",
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

	// ── terrapod_inventory_refresh ───────────────────────────────────
	//
	// Named "refresh" rather than "resolve" on purpose: `terrapod_inventory_
	// resolve` sits one letter from the read-only `terrapod_inventory_resolved`,
	// and a write tool an agent can reach for by near-miss is a bad trade for
	// matching the route name.
	type inventoryRefreshIn struct {
		InventoryID string `json:"inventory_id" jsonschema:"the inventory id, from terrapod_workspace_inventories"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_refresh",
		Description: "Re-resolve an inventory now and record a new snapshot, then return it. This is how a stale target set is brought up to date — the limit preview is only as fresh as the latest snapshot, so an agent that finds `taken-at` old refreshes before previewing. " +
			"It writes a snapshot record; it declares no hosts and touches no infrastructure, and the hosts it reports come from the sources as they already are. " +
			"Requires inventory write on the workspace. Only works for an inventory Terrapod can resolve itself: one carrying a source that needs ansible answers a conflict naming the offending kinds, because the API will not resolve the rest of it — a partial resolution is a target set that is silently too small, and a runner has to do that one.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in inventoryRefreshIn) (*mcp.CallToolResult, *terrapod.InventoryVersion, error) {
		if in.InventoryID == "" {
			return errText("inventory_id is required"), nil, nil
		}
		version, err := c.ResolveInventory(ctx, in.InventoryID)
		if err != nil {
			return errResult(err), nil, nil
		}
		// The ansible-shaped rendering is the lossy one (a var-less host is
		// omitted from it), and a refresh is about freshness rather than the
		// document, so the exhaustive maps are what comes back here.
		version.AnsibleInventory = nil
		return nil, version, nil
	})
}
