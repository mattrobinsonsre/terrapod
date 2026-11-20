package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerAutodiscovery adds the workspace-autodiscovery rule tools.
//
// A rule is the Atlantis-shaped half of Terrapod: it watches a repository and
// creates a workspace per matching directory as those directories appear, so
// a monorepo does not need a hand-written workspace per service. The MODULE
// autodiscovery tools in ground.go are the sibling for the registry; these are
// for workspaces, and the two are easy to confuse — every description here says
// "workspace" explicitly for that reason.
//
// There were no tools for this at all until #2621: eight API routes, 116 tools,
// and none of them these — so an agent could read a rule's effects (the
// workspaces it had created) and nothing about the rule. Bounded by the caller's
// RBAC like every other tool; the routes are platform-admin.
func registerAutodiscovery(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_autodiscovery_rule_list ─────────────────────────────
	type ruleListOut struct {
		Count int                          `json:"count"`
		Rules []terrapod.AutodiscoveryRule `json:"rules"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_list",
		Description: "List the WORKSPACE autodiscovery rules: each watches one repository and creates a workspace per directory matching its `pattern`, as those directories appear. " +
			"`attributes` carries the whole rule — the VCS wiring, the pattern and ignore paths, the naming template, and the settings every workspace it creates inherits (engine, " +
			"execution mode, agent pool, auto-apply, the Ansible and Pulumi roots, policy and scanning). Use it to find out why a workspace exists, or what a rule would stamp on the " +
			"next one. The sibling for registry modules is terrapod_module_autodiscovery_rule_list. Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, ruleListOut, error) {
		rules, err := c.ListAllAutodiscoveryRules(ctx)
		if err != nil {
			return errResult(err), ruleListOut{}, nil
		}
		return nil, ruleListOut{Count: len(rules), Rules: rules}, nil
	})

	// ── terrapod_autodiscovery_rule_get ──────────────────────────────
	type ruleGetIn struct {
		RuleID string `json:"rule_id" jsonschema:"the workspace autodiscovery rule id (ad-...)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_get",
		Description: "One WORKSPACE autodiscovery rule in full, including every setting it templates onto the workspaces it creates. Read this before changing a rule: a rule's settings are " +
			"applied to NEW workspaces at creation, and separately propagated to the ones it already created when an update asks for it (see terrapod_autodiscovery_rule_update). " +
			"Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in ruleGetIn) (*mcp.CallToolResult, *terrapod.AutodiscoveryRule, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), nil, nil
		}
		rule, err := c.GetAutodiscoveryRule(ctx, in.RuleID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, rule, nil
	})

	// ── terrapod_autodiscovery_rule_preview ──────────────────────────
	type rulePreviewIn struct {
		RuleID string `json:"rule_id,omitempty" jsonschema:"preview a SAVED rule by id (ad-...). Give this or attributes, not both"`
		// The unsaved form takes the same attribute map as create, so an
		// agent can see what a rule would do before committing to it.
		Attributes map[string]any `json:"attributes,omitempty" jsonschema:"preview an UNSAVED rule from the attributes that would create it (same shape as terrapod_autodiscovery_rule_create). Give this or rule_id, not both"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_preview",
		Description: "Dry-run a WORKSPACE autodiscovery rule: walk the repository and report the workspace each matching directory would produce — its name, its working directory, whether " +
			"the name is already taken by a hand-made workspace (`collision`, which the rule will NOT take over) and whether this rule already created it (`existing_autodiscovered`). " +
			"`ref` is the branch actually walked and `files-walked` how many paths the walk returned, so an empty result distinguishes 'nothing matched' from 'nothing to match'. " +
			"Give `rule_id` for a saved rule, or `attributes` to try a rule that does not exist yet. CREATES NOTHING — use terrapod_autodiscovery_rule_scan to act on it. Platform admin only.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in rulePreviewIn) (*mcp.CallToolResult, *terrapod.AutodiscoveryRulePreview, error) {
		switch {
		case in.RuleID == "" && len(in.Attributes) == 0:
			return errText("give either rule_id (a saved rule) or attributes (an unsaved one)"), nil, nil
		case in.RuleID != "" && len(in.Attributes) > 0:
			return errText("give rule_id or attributes, not both — they preview different rules"), nil, nil
		case in.RuleID != "":
			p, err := c.PreviewAutodiscoveryRule(ctx, in.RuleID)
			if err != nil {
				return errResult(err), nil, nil
			}
			return nil, p, nil
		default:
			p, err := c.PreviewUnsavedAutodiscoveryRule(ctx, in.Attributes)
			if err != nil {
				return errResult(err), nil, nil
			}
			return nil, p, nil
		}
	})

	// ── terrapod_autodiscovery_rule_create ───────────────────────────
	type ruleCreateIn struct {
		Attributes map[string]any `json:"attributes" jsonschema:"the rule's attributes, by their wire (kebab-case) names. Required: name, vcs-connection-id, repo-url, pattern. The rest template onto the workspaces it creates — read an existing rule with terrapod_autodiscovery_rule_get for the full set, and dry-run with terrapod_autodiscovery_rule_preview first"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_create",
		Description: "Create a WORKSPACE autodiscovery rule. The rule then creates a workspace per matching directory — so this is a standing instruction to create workspaces, not a one-off: " +
			"dry-run it with terrapod_autodiscovery_rule_preview (passing `attributes`) before creating it. Attribute names are the kebab-case wire names; the server rejects an unknown one " +
			"with a 422 naming it, and refuses a reserved label key. Platform admin only.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in ruleCreateIn) (*mcp.CallToolResult, *terrapod.AutodiscoveryRule, error) {
		if len(in.Attributes) == 0 {
			return errText("attributes is required"), nil, nil
		}
		rule, err := c.CreateAutodiscoveryRule(ctx, in.Attributes)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, rule, nil
	})

	// ── terrapod_autodiscovery_rule_update ───────────────────────────
	type ruleUpdateIn struct {
		RuleID     string         `json:"rule_id" jsonschema:"the rule id (ad-...)"`
		Attributes map[string]any `json:"attributes" jsonschema:"the attributes to change, by their wire (kebab-case) names. Attributes you omit are left alone"`
		// `propagate` and `dry_run` are request OPTIONS rather than rule
		// attributes, which is why they are their own fields: they change
		// what the update DOES, not what the rule says.
		Propagate bool `json:"propagate,omitempty" jsonschema:"also apply the settings THIS update changes to the workspaces the rule already created. Off by default, because a rule's settings normally apply only to workspaces it creates from now on. Only the fields this call changes are pushed, so an update that changes nothing propagates nothing"`
		DryRun    bool `json:"dry_run,omitempty" jsonschema:"with propagate: report what propagation WOULD do and persist neither the rule nor any workspace"`
	}
	type ruleUpdateOut struct {
		Rule *terrapod.AutodiscoveryRule `json:"rule"`
		// Propagation is absent unless propagate was asked for.
		Propagation *terrapod.Propagation `json:"propagation,omitempty"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_update",
		Description: "Change a WORKSPACE autodiscovery rule. By default the change affects only the workspaces the rule creates FROM NOW ON; set `propagate` to also push the settings this " +
			"call changes onto the ones it has already created. Propagation is scoped twice over, so it cannot clobber anything: only the fields this update changes are considered, and of " +
			"those, a field is written only where the workspace STILL HOLDS the rule's previous value. Where someone has since changed it by hand the field is reported under `customised` and " +
			"LEFT ALONE — propagation never overwrites a deliberate local change. The report says what happened either way: `matched` is how many workspaces the rule reaches, `would_change` " +
			"what was or would be written, `customised` what was skipped and why (current, previous and intended value per field), `unchanged` the ones already matching, and " +
			"`skipped_lifecycle` those a lifecycle state excludes. Set `dry_run` with `propagate` to get that report without persisting anything. Platform admin only.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in ruleUpdateIn) (*mcp.CallToolResult, ruleUpdateOut, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), ruleUpdateOut{}, nil
		}
		if len(in.Attributes) == 0 {
			return errText("attributes is required"), ruleUpdateOut{}, nil
		}
		// A dry run without propagate would be a silent no-op that reads
		// as a preview, so refuse rather than quietly persisting.
		if in.DryRun && !in.Propagate {
			return errText("dry_run applies to propagation — set propagate too, or drop dry_run"), ruleUpdateOut{}, nil
		}
		if in.Propagate {
			rule, report, err := c.UpdateAutodiscoveryRuleWithPropagation(
				ctx, in.RuleID, in.Attributes, terrapod.PropagateOptions{DryRun: in.DryRun},
			)
			if err != nil {
				return errResult(err), ruleUpdateOut{}, nil
			}
			return nil, ruleUpdateOut{Rule: rule, Propagation: report}, nil
		}
		rule, err := c.UpdateAutodiscoveryRule(ctx, in.RuleID, in.Attributes)
		if err != nil {
			return errResult(err), ruleUpdateOut{}, nil
		}
		return nil, ruleUpdateOut{Rule: rule}, nil
	})

	// ── terrapod_autodiscovery_rule_scan ─────────────────────────────
	type ruleScanIn struct {
		RuleID string `json:"rule_id" jsonschema:"the rule id (ad-...)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_scan",
		Description: "Scan a WORKSPACE autodiscovery rule's repository now and CREATE the workspaces it finds. This writes: use terrapod_autodiscovery_rule_preview first to see what it would do. " +
			"`workspaces-created` and `workspace-ids` cover newly created workspaces only, so a rule that has already materialised everything it matches reports zero — that is success, not a " +
			"no-op to investigate. It runs even on a disabled rule, deliberately, so an on-demand scan does not silently do nothing because the rule's own polling is switched off. Platform admin only.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in ruleScanIn) (*mcp.CallToolResult, *terrapod.AutodiscoveryScan, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), nil, nil
		}
		scan, err := c.ScanAutodiscoveryRule(ctx, in.RuleID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, scan, nil
	})

	// ── terrapod_autodiscovery_rule_delete ───────────────────────────
	type ruleDeleteIn struct {
		RuleID string `json:"rule_id" jsonschema:"the rule id (ad-...)"`
	}
	type ruleDeleteOut struct {
		Deleted bool   `json:"deleted"`
		RuleID  string `json:"rule_id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_autodiscovery_rule_delete",
		Description: "Delete a WORKSPACE autodiscovery rule. The workspaces it already created are LEFT IN PLACE — deleting the rule stops it creating more and stops it propagating settings; " +
			"it does not remove anything it has made. Platform admin only.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in ruleDeleteIn) (*mcp.CallToolResult, ruleDeleteOut, error) {
		if in.RuleID == "" {
			return errText("rule_id is required"), ruleDeleteOut{}, nil
		}
		if err := c.DeleteAutodiscoveryRule(ctx, in.RuleID); err != nil {
			return errResult(err), ruleDeleteOut{}, nil
		}
		return nil, ruleDeleteOut{Deleted: true, RuleID: in.RuleID}, nil
	})
}
