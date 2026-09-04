package mcpserver

import (
	"context"
	"errors"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerCRUD adds the workspace + variable management tools — the config
// surface an agent needs to *shape* the estate, distinct from the run-lifecycle
// Act tools. Everything is an ordinary go-terrapod call bounded by the user's
// RBAC: a create/update/delete only succeeds if the token's capabilities allow
// it. Mutating tools that remove config or infrastructure carry the
// `destructive` hint so an MCP host prompts for confirmation, mirroring the
// UI's confirm-on-destructive-action policy.
func registerCRUD(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_workspace_create ────────────────────────────────────
	type workspaceCreateIn struct {
		Name             string            `json:"name" jsonschema:"the workspace name (unique within the org)"`
		ExecutionMode    string            `json:"execution_mode,omitempty" jsonschema:"local or agent (default: server default)"`
		ExecutionBackend string            `json:"execution_backend,omitempty" jsonschema:"tofu or terraform (default: server default)"`
		TerraformVersion string            `json:"terraform_version,omitempty" jsonschema:"partial version like 1.15 (means 1.15.*); no HCL operators"`
		AutoApply        *bool             `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans (default false)"`
		AutoApplyMode    *string           `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create (only plans that add resources), create_update (also in-place updates). create and create_update never auto-apply a destroy or replace. Set this OR auto_apply, not both."`
		AgentPoolID      string            `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...) for agent execution mode; assigns exactly one pool. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs     []string          `json:"agent_pool_ids,omitempty" jsonschema:"agent pools this workspace may run on (apool-...). Flat set: a run is offered to every pool at once and whichever has a live runner claims it first, so losing one does not stop the workspace. Mutually exclusive with agent_pool_id"`
		WorkingDirectory string            `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		VCSConnectionID  string            `json:"vcs_connection_id,omitempty" jsonschema:"VCS connection id to wire this workspace to a repo"`
		VCSRepoURL       string            `json:"vcs_repo_url,omitempty" jsonschema:"git repo URL (requires vcs_connection_id)"`
		VCSBranch        string            `json:"vcs_branch,omitempty" jsonschema:"tracked branch (empty = repo default)"`
		OwnerEmail       string            `json:"owner_email,omitempty" jsonschema:"workspace owner email (defaults to the caller)"`
		Labels           map[string]string `json:"labels,omitempty" jsonschema:"key/value labels for RBAC + filtering (reserved keys rejected)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_create",
		Description: "Create a workspace. Only `name` is required; everything else falls back to the instance default. " +
			"For agent execution set execution_mode=agent + agent_pool_id; for VCS-driven runs set vcs_connection_id + vcs_repo_url. Returns the created workspace.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceCreateIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.Name == "" {
			return errText("name is required"), nil, nil
		}
		ws, err := c.CreateWorkspace(ctx, terrapod.CreateWorkspaceRequest{
			Name:             in.Name,
			ExecutionMode:    in.ExecutionMode,
			ExecutionBackend: in.ExecutionBackend,
			TerraformVersion: in.TerraformVersion,
			AutoApply:        in.AutoApply,
			AutoApplyMode:    in.AutoApplyMode,
			AgentPoolID:      in.AgentPoolID,
			AgentPoolIDs:     in.AgentPoolIDs,
			WorkingDirectory: in.WorkingDirectory,
			VCSConnectionID:  in.VCSConnectionID,
			VCSRepoURL:       in.VCSRepoURL,
			VCSBranch:        in.VCSBranch,
			OwnerEmail:       in.OwnerEmail,
			Labels:           in.Labels,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, ws, nil
	})

	// ── terrapod_workspace_update ────────────────────────────────────
	type workspaceUpdateIn struct {
		WorkspaceID      string            `json:"workspace_id" jsonschema:"the workspace id (ws-...) to update"`
		Name             string            `json:"name,omitempty" jsonschema:"rename the workspace (empty = leave)"`
		ExecutionMode    string            `json:"execution_mode,omitempty" jsonschema:"local or agent"`
		ExecutionBackend string            `json:"execution_backend,omitempty" jsonschema:"tofu or terraform"`
		TerraformVersion string            `json:"terraform_version,omitempty" jsonschema:"partial version like 1.15"`
		AutoApply        *bool             `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans"`
		AutoApplyMode    *string           `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create, create_update. Set this OR auto_apply, not both."`
		AgentPoolID      string            `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...); assigns exactly one pool, REPLACING any existing set. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs     []string          `json:"agent_pool_ids,omitempty" jsonschema:"replace the workspace''s agent-pool set (apool-...). Flat set — every pool is equally eligible to claim a run. Mutually exclusive with agent_pool_id"`
		WorkingDirectory string            `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		Labels           map[string]string `json:"labels,omitempty" jsonschema:"replace the label set (reserved keys rejected)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_update",
		Description: "Update a workspace's settings. Only the fields you pass change; omitted fields are left alone. " +
			"This is a config change (the new settings apply on the workspace's next run) — it does not itself queue a run.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceUpdateIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		ws, err := c.UpdateWorkspace(ctx, in.WorkspaceID, terrapod.UpdateWorkspaceRequest{
			Name:             in.Name,
			ExecutionMode:    in.ExecutionMode,
			ExecutionBackend: in.ExecutionBackend,
			TerraformVersion: in.TerraformVersion,
			AutoApply:        in.AutoApply,
			AutoApplyMode:    in.AutoApplyMode,
			AgentPoolID:      in.AgentPoolID,
			AgentPoolIDs:     in.AgentPoolIDs,
			WorkingDirectory: in.WorkingDirectory,
			Labels:           in.Labels,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, ws, nil
	})

	// ── terrapod_workspace_delete ────────────────────────────────────
	type workspaceDeleteIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) to delete"`
	}
	type deleteOut struct {
		Deleted bool   `json:"deleted"`
		ID      string `json:"id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_workspace_delete",
		Description: "Delete a workspace and its Terrapod-side records (variables, run history, state versions). This does NOT destroy the real infrastructure the state tracks — to tear that down, queue a destroy run first (terrapod_run_create is_destroy=true) and apply it. Catalog-managed workspaces are rejected (409); destroy the catalog instance instead. Confirm with the user before calling it. The state blobs outlive the delete for a limited window (default 30 days), so a platform admin can SALVAGE them with terrapod_deleted_workspace_restore — but that is a recovery operation, not an undo: it yields a NEW workspace with a new id, it comes back inert with auto-apply and VCS off, the variables and run history do not come back, and once the window passes the state is reaped for good. Do not offer the delete as something easily reversed.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceDeleteIn) (*mcp.CallToolResult, *deleteOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		if err := c.DeleteWorkspace(ctx, in.WorkspaceID); err != nil {
			return errResult(err), nil, nil
		}
		return nil, &deleteOut{Deleted: true, ID: in.WorkspaceID}, nil
	})

	// ── terrapod_variable_list ───────────────────────────────────────
	type variableListIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose variables to list"`
	}
	type variableListOut struct {
		Count     int                 `json:"count"`
		Variables []terrapod.Variable `json:"variables"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_variable_list",
		Description: "List a workspace's variables (terraform + env). Sensitive values are masked by the server (never returned). Use to inspect config before a run or before setting a variable.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in variableListIn) (*mcp.CallToolResult, variableListOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), variableListOut{}, nil
		}
		vars, err := c.ListAllVariables(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), variableListOut{}, nil
		}
		return nil, variableListOut{Count: len(vars), Variables: vars}, nil
	})

	// ── terrapod_workspace_varsets ───────────────────────────────────
	type workspaceVarsetsIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose variable sets to list"`
	}
	type workspaceVarsetsOut struct {
		Count   int                        `json:"count"`
		Varsets []terrapod.WorkspaceVarset `json:"varsets"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_varsets",
		Description: "List the variable sets applying to a workspace, and how each one came to apply " +
			"(explicit assignment, global, or matched by an assignment rule). " +
			"terrapod_variable_list shows only the workspace's own variables, so when a run sees a " +
			"variable that is not among them, it came from one of these sets.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceVarsetsIn) (*mcp.CallToolResult, workspaceVarsetsOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), workspaceVarsetsOut{}, nil
		}
		sets, err := c.ListWorkspaceVarsets(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), workspaceVarsetsOut{}, nil
		}
		return nil, workspaceVarsetsOut{Count: len(sets), Varsets: sets}, nil
	})

	// ── terrapod_variable_set ────────────────────────────────────────
	type variableSetIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...)"`
		Key         string `json:"key" jsonschema:"the variable key"`
		Value       string `json:"value,omitempty" jsonschema:"the value (empty is legal, e.g. flag-shaped env vars)"`
		Category    string `json:"category,omitempty" jsonschema:"terraform, env, git_http_auth, or git_ssh_auth (default terraform); the git_* categories carry private-git-module credentials as a JSON value and are always sensitive"`
		Structured  *bool  `json:"structured,omitempty" jsonschema:"the value is a typed expression rather than a plain string (lists/objects/numbers/bools); default false"`
		HCL         *bool  `json:"hcl,omitempty" jsonschema:"deprecated alias for structured; both are the same flag"`
		Sensitive   *bool  `json:"sensitive,omitempty" jsonschema:"mark sensitive — masked at rest and in responses; default false"`
		Description string `json:"description,omitempty" jsonschema:"optional human description"`
		ValueSource string `json:"value_source,omitempty" jsonschema:"static (default — value is the literal) or vault, where value is a JSON reference {\"mount\":…,\"path\":…,\"field\":…} that Terrapod reads from HashiCorp Vault at run time; a vault-sourced variable is always sensitive and the secret is never stored in Terrapod"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_variable_set",
		Description: "Set a workspace variable — creates it if the key is new, updates it in place if it exists (an upsert keyed on `key`). " +
			"category defaults to terraform; set category=env for an environment variable, or git_http_auth/git_ssh_auth for private-git-module credentials (JSON value, always sensitive — see the module-auth docs). Set structured=true for non-string values (lists/objects/numbers); `hcl` is its deprecated alias. " +
			"Set value_source=vault to store a Vault reference instead of a literal, so the secret stays in Vault and is read per run — an unresolvable reference fails the run rather than delivering nothing. Returns the variable.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in variableSetIn) (*mcp.CallToolResult, *terrapod.Variable, error) {
		if in.WorkspaceID == "" || in.Key == "" {
			return errText("workspace_id and key are required"), nil, nil
		}
		category := in.Category
		if category == "" {
			category = "terraform"
		}
		// Upsert: look the key up; update if present, else create. A NotFound on
		// the lookup is the create path, not an error.
		existing, err := c.GetVariableByKey(ctx, in.WorkspaceID, in.Key)
		switch {
		case err == nil && existing != nil:
			v, uerr := c.UpdateVariable(ctx, in.WorkspaceID, existing.ID, terrapod.UpdateVariableRequest{
				Value:       &in.Value,
				Category:    category,
				Structured:  firstSet(in.Structured, in.HCL),
				Sensitive:   in.Sensitive,
				Description: strPtrOrNil(in.Description),
				ValueSource: strPtrOrNil(in.ValueSource),
			})
			if uerr != nil {
				return errResult(uerr), nil, nil
			}
			return nil, v, nil
		case err != nil && !isNotFound(err):
			return errResult(err), nil, nil
		}
		v, cerr := c.CreateVariable(ctx, in.WorkspaceID, terrapod.CreateVariableRequest{
			Key:         in.Key,
			Value:       in.Value,
			Category:    category,
			Structured:  boolOrFalse(firstSet(in.Structured, in.HCL)),
			Sensitive:   boolOrFalse(in.Sensitive),
			Description: in.Description,
			ValueSource: in.ValueSource,
		})
		if cerr != nil {
			return errResult(cerr), nil, nil
		}
		return nil, v, nil
	})

	// ── terrapod_variable_delete ─────────────────────────────────────
	type variableDeleteIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...)"`
		Key         string `json:"key" jsonschema:"the variable key to delete"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_variable_delete",
		Description: "Delete a workspace variable by key. Irreversible (the value, if not sensitive, is gone) and it changes what the next run sees — confirm with the user.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in variableDeleteIn) (*mcp.CallToolResult, *deleteOut, error) {
		if in.WorkspaceID == "" || in.Key == "" {
			return errText("workspace_id and key are required"), nil, nil
		}
		existing, err := c.GetVariableByKey(ctx, in.WorkspaceID, in.Key)
		if err != nil {
			return errResult(err), nil, nil
		}
		if err := c.DeleteVariable(ctx, in.WorkspaceID, existing.ID); err != nil {
			return errResult(err), nil, nil
		}
		return nil, &deleteOut{Deleted: true, ID: existing.ID}, nil
	})
}

// isNotFound reports whether err is a go-terrapod not-found (the upsert
// create-path signal), without conflating it with auth/other errors.
func isNotFound(err error) bool {
	var nf *terrapod.NotFoundError
	return errors.As(err, &nf)
}

func boolOrFalse(b *bool) bool { return b != nil && *b }

func strPtrOrNil(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// firstSet prefers the new name and falls back to its deprecated alias, so an
// agent written against either keeps working (#1435).
func firstSet(preferred, fallback *bool) *bool {
	if preferred != nil {
		return preferred
	}
	return fallback
}
