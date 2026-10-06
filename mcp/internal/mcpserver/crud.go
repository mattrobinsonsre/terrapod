package mcpserver

import (
	"context"
	"errors"
	"fmt"
	"strings"

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
// engineVersionIn resolves the two names an agent may use for the engine
// version (#1559), returning the version to send and an error message when the
// call disagrees with itself.
//
// `engine_version` is canonical; `terraform_version` is the name the terraform
// CLI tooling uses, which the API accepts indefinitely. Because only one key
// reaches the server, a disagreeing pair would otherwise be resolved silently —
// so it is refused here, matching the 422 the server gives a client that sends
// both.
func engineVersionIn(engine, terraform string) (version, errMsg string) {
	if engine != "" && terraform != "" && engine != terraform {
		return "", "engine_version and terraform_version are the same version under two " +
			"names and disagree; pass either one, or both with the same value"
	}
	if engine != "" {
		return engine, ""
	}
	return terraform, ""
}

func registerCRUD(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_workspace_create ────────────────────────────────────
	type workspaceCreateIn struct {
		Name             string              `json:"name" jsonschema:"the workspace name (unique within the org)"`
		Engine           string              `json:"engine,omitempty" jsonschema:"the execution engine family: terraform (default) or another engine this deployment enables, such as pulumi. NOT execution_backend, which picks tofu vs terraform within the Terraform engine. Workspaces are created here, in the UI or with the Terraform provider — never by an engine's own CLI"`
		ExecutionMode    string              `json:"execution_mode,omitempty" jsonschema:"local or agent (default: server default)"`
		ExecutionBackend string              `json:"execution_backend,omitempty" jsonschema:"which binary runs a Terraform-engine workspace: tofu or terraform. A choice WITHIN the Terraform engine — Pulumi has one binary, so this has no meaning on a pulumi workspace (default: server default)"`
		EngineVersion    string              `json:"engine_version,omitempty" jsonschema:"version of the engine this workspace runs; partial like 1.15 (means 1.15.*), no HCL operators"`
		TerraformVersion string              `json:"terraform_version,omitempty" jsonschema:"the same version under its original name; prefer engine_version. Setting both to different values is rejected"`
		AutoApply        *bool               `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans (default false)"`
		AutoApplyMode    *string             `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create (only plans that add resources), create_update (also in-place updates). create and create_update never auto-apply a destroy or replace. Set this OR auto_apply, not both."`
		AgentPoolID      string              `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...) for agent execution mode; assigns exactly one pool. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs     []string            `json:"agent_pool_ids,omitempty" jsonschema:"agent pools this workspace may run on (apool-...). Flat set: a run is offered to every pool at once and whichever has a live runner claims it first, so losing one does not stop the workspace. Mutually exclusive with agent_pool_id"`
		WorkingDirectory string              `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		OIDCAudiences    map[string][]string `json:"oidc_audiences,omitempty" jsonschema:"the per-provider audiences this workspace's run identity tokens are minted with -- its cloud identity opt-in (#1901). Omit it (the default) and the workspace overrides nothing, inheriting the deployment's catalogue; a deployment that configures no audiences mints nothing and its runs authenticate with the agent pool's own identity, exactly as before. A MAP keyed on the provider configuration a token is for, never a flat list: the bare provider type exactly as a provider block writes it (aws, vault), or type.alias for one aliased configuration (aws.west). The alias is part of the KEY -- do not split on the dot and do not fold aws.west into aws; they are separate targets, and the runner asks for whichever its configuration actually uses. Each value is ALWAYS a list even for a single entry, because a federation target's audience is one value and a list of several is a deliberate these-are-interchangeable statement; the runner mints one token per target carrying only that target's audiences. An audience is an opaque string the federation target itself names -- whatever your cloud's or secret store's trust configuration expects -- and Terrapod stores it verbatim; nothing here is specific to any one cloud, because Terrapod only mints an OIDC JWT and the runner writes it to a file. What you pass is this workspace's OVERRIDE, merged per key OVER the deployment's own catalogue, so naming one target does not restate the rest. The read side differs and this is the trap: a workspace reports its oidc-audiences as the MERGED view, so writing back what you read would promote every inherited entry into an override. A key whose list is EMPTY is refused (422) -- express no-audiences-for-this-target by leaving the key out, which falls back to the deployment's value, because an empty list is indistinguishable from a typo. A token audienced for two targets is replayable between them, so give a target only its own audiences. Setting this does NOT by itself grant anything: the federation target's own trust policy decides what a token bearing these audiences may do."`
		VCSConnectionID  string              `json:"vcs_connection_id,omitempty" jsonschema:"VCS connection id to wire this workspace to a repo"`
		VCSRepoURL       string              `json:"vcs_repo_url,omitempty" jsonschema:"git repo URL (requires vcs_connection_id)"`
		VCSBranch        string              `json:"vcs_branch,omitempty" jsonschema:"tracked branch (empty = repo default)"`
		AllowForkPRPlans *bool               `json:"allow_fork_pr_plans,omitempty" jsonschema:"allow a pull request opened from a FORK to get a speculative plan. **Off by default**, which is the safe setting: such a plan runs the fork author's code with this workspace's full credential set (env variables, secret-manager values, git credentials, the runner's cloud identity), and that author has no write access and cannot merge, so the plan is the only path by which their code reaches those credentials. Pull requests from branches in the repository itself always plan and are unaffected. Set it true only on a workspace that holds nothing worth taking, and note that an autodiscovery rule carries its own value for the workspaces it creates (GHSA-gp5w-76rw-c452)"`
		OwnerEmail       string              `json:"owner_email,omitempty" jsonschema:"workspace owner email (defaults to the caller)"`
		Labels           map[string]string   `json:"labels,omitempty" jsonschema:"key/value labels for RBAC + filtering (reserved keys rejected)"`
		PulumiBindPlan   *bool               `json:"pulumi_bind_plan,omitempty" jsonschema:"Pulumi workspaces only: bind the update to the approved preview (preview --save-plan then up --plan). Off by default; setting it true is rejected on any other engine"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_create",
		Description: "Create a workspace. Only `name` is required; everything else falls back to the instance default. " +
			"For agent execution set execution_mode=agent + agent_pool_id; for VCS-driven runs set vcs_connection_id + vcs_repo_url. " +
			"For a Pulumi workspace set engine=pulumi, and pulumi_bind_plan to bind its update to the approved preview. Returns the created workspace.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceCreateIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.Name == "" {
			return errText("name is required"), nil, nil
		}
		version, verr := engineVersionIn(in.EngineVersion, in.TerraformVersion)
		if verr != "" {
			return errText(verr), nil, nil
		}
		ws, err := c.CreateWorkspace(ctx, terrapod.CreateWorkspaceRequest{
			Name:             in.Name,
			Engine:           in.Engine,
			ExecutionMode:    in.ExecutionMode,
			ExecutionBackend: in.ExecutionBackend,
			EngineVersion:    version,
			AutoApply:        in.AutoApply,
			AutoApplyMode:    in.AutoApplyMode,
			AgentPoolID:      in.AgentPoolID,
			AgentPoolIDs:     in.AgentPoolIDs,
			WorkingDirectory: in.WorkingDirectory,
			VCSConnectionID:  in.VCSConnectionID,
			VCSRepoURL:       in.VCSRepoURL,
			VCSBranch:        in.VCSBranch,
			OIDCAudiences:    in.OIDCAudiences,
			OwnerEmail:       in.OwnerEmail,
			Labels:           in.Labels,
			// Passed straight through rather than pre-checked against
			// `in.Engine`: the server refuses true on a non-Pulumi engine, and
			// a second copy of that rule here would be one to keep in step for
			// no gain — and would answer for an engine list only the server
			// knows. Omitted when unset, so a Terraform create is unchanged.
			PulumiBindPlan: in.PulumiBindPlan,
			// A pointer all the way through, so "leave it alone" stays
			// distinguishable from "turn it off" (GHSA-gp5w-76rw-c452). Unset
			// is omitted from the request, leaving the server's default — which
			// is OFF — rather than asserting a value over whatever the operator
			// chose.
			AllowForkPRPlans: in.AllowForkPRPlans,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, ws, nil
	})

	// ── terrapod_workspace_update ────────────────────────────────────
	type workspaceUpdateIn struct {
		WorkspaceID      string              `json:"workspace_id" jsonschema:"the workspace id (ws-...) to update"`
		Name             string              `json:"name,omitempty" jsonschema:"rename the workspace (empty = leave)"`
		ExecutionMode    string              `json:"execution_mode,omitempty" jsonschema:"local or agent"`
		ExecutionBackend string              `json:"execution_backend,omitempty" jsonschema:"which binary runs a Terraform-engine workspace: tofu or terraform. A choice WITHIN the Terraform engine — Pulumi has one binary, so this has no meaning on a pulumi workspace"`
		EngineVersion    string              `json:"engine_version,omitempty" jsonschema:"version of the engine this workspace runs; partial like 1.15 (means 1.15.*)"`
		TerraformVersion string              `json:"terraform_version,omitempty" jsonschema:"the same version under its original name; prefer engine_version. Setting both to different values is rejected"`
		AutoApply        *bool               `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans"`
		AutoApplyMode    *string             `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create, create_update. Set this OR auto_apply, not both."`
		AgentPoolID      string              `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...); assigns exactly one pool, REPLACING any existing set. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs     []string            `json:"agent_pool_ids,omitempty" jsonschema:"replace the workspace''s agent-pool set (apool-...). Flat set — every pool is equally eligible to claim a run. Mutually exclusive with agent_pool_id"`
		WorkingDirectory string              `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		Labels           map[string]string   `json:"labels,omitempty" jsonschema:"replace the label set (reserved keys rejected)"`
		PulumiBindPlan   *bool               `json:"pulumi_bind_plan,omitempty" jsonschema:"Pulumi workspaces only: bind the update to the approved preview (preview --save-plan then up --plan). Off by default; rejected on any other engine"`
		AllowForkPRPlans *bool               `json:"allow_fork_pr_plans,omitempty" jsonschema:"allow a pull request opened from a FORK to get a speculative plan. **Off by default**, which is the safe setting: such a plan runs the fork author's code with this workspace's full credential set (env variables, secret-manager values, git credentials, the runner's cloud identity), and that author has no write access and cannot merge, so the plan is the only path by which their code reaches those credentials. Pull requests from branches in the repository itself always plan and are unaffected. Set it true only on a workspace that holds nothing worth taking, and note that an autodiscovery rule carries its own value for the workspaces it creates (GHSA-gp5w-76rw-c452)"`
		OIDCAudiences    map[string][]string `json:"oidc_audiences,omitempty" jsonschema:"replace this workspace's per-provider audience overrides for its run identity tokens -- its cloud identity opt-in (#1901). Omitting the field leaves the existing overrides alone; pass an EMPTY object to clear every one of them, which drops the workspace back to inheriting the deployment's catalogue wholesale. A MAP keyed on the provider configuration a token is for, never a flat list: the bare provider type exactly as a provider block writes it (aws, vault), or type.alias for one aliased configuration (aws.west). The alias is part of the KEY -- do not split on the dot and do not fold aws.west into aws; they are separate targets, and the runner asks for whichever its configuration actually uses. Each value is ALWAYS a list even for a single entry, because a federation target's audience is one value and a list of several is a deliberate these-are-interchangeable statement; the runner mints one token per target carrying only that target's audiences. An audience is an opaque string the federation target itself names -- whatever your cloud's or secret store's trust configuration expects -- and Terrapod stores it verbatim; nothing here is specific to any one cloud, because Terrapod only mints an OIDC JWT and the runner writes it to a file. What you pass is this workspace's OVERRIDE, merged per key OVER the deployment's own catalogue, so naming one target does not restate the rest. The read side differs and this is the trap: a workspace reports its oidc-audiences as the MERGED view, so writing back what you read would promote every inherited entry into an override. A key whose list is EMPTY is refused (422) -- express no-audiences-for-this-target by leaving the key out, which falls back to the deployment's value, because an empty list is indistinguishable from a typo. A token audienced for two targets is replayable between them, so give a target only its own audiences. Setting this does NOT by itself grant anything: the federation target's own trust policy decides what a token bearing these audiences may do."`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_update",
		Description: "Update a workspace's settings. Only the fields you pass change; omitted fields are left alone. " +
			"This is a config change (the new settings apply on the workspace's next run) — it does not itself queue a run.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in workspaceUpdateIn) (*mcp.CallToolResult, *terrapod.Workspace, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		version, verr := engineVersionIn(in.EngineVersion, in.TerraformVersion)
		if verr != "" {
			return errText(verr), nil, nil
		}
		ws, err := c.UpdateWorkspace(ctx, in.WorkspaceID, terrapod.UpdateWorkspaceRequest{
			Name:             in.Name,
			ExecutionMode:    in.ExecutionMode,
			ExecutionBackend: in.ExecutionBackend,
			EngineVersion:    version,
			AutoApply:        in.AutoApply,
			AutoApplyMode:    in.AutoApplyMode,
			AgentPoolID:      in.AgentPoolID,
			AgentPoolIDs:     in.AgentPoolIDs,
			WorkingDirectory: in.WorkingDirectory,
			Labels:           in.Labels,
			PulumiBindPlan:   in.PulumiBindPlan,
			OIDCAudiences:    in.OIDCAudiences,
			AllowForkPRPlans: in.AllowForkPRPlans,
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
		Category    string `json:"category,omitempty" jsonschema:"terraform, env, git_http_auth, or git_ssh_auth (default terraform); native is accepted as an equivalent name for terraform. terraform is the engine's own parameter channel -- Terraform input variables, Pulumi stack config, Ansible extra vars -- one category, delivered by whichever engine the workspace runs; Terrapod's own API calls it native, and this tool reads the compatibility surface, which calls it terraform. The git_* categories carry private-git-module credentials as a JSON value and are always sensitive"`
		Structured  *bool  `json:"structured,omitempty" jsonschema:"the value is a typed expression rather than a plain string (lists/objects/numbers/bools); default false"`
		HCL         *bool  `json:"hcl,omitempty" jsonschema:"deprecated alias for structured; both are the same flag"`
		Sensitive   *bool  `json:"sensitive,omitempty" jsonschema:"mark sensitive — masked at rest and in responses; default false"`
		Description string `json:"description,omitempty" jsonschema:"optional human description"`
		ValueSource string `json:"value_source,omitempty" jsonschema:"static (default — value is the literal) or vault, where value is a JSON reference {\"mount\":…,\"path\":…,\"field\":…} that Terrapod reads from OpenBao (or HashiCorp Vault) at run time; a vault-sourced variable is always sensitive and the secret is never stored in Terrapod. Add \"file\":{\"name\":\"gcp/adc.json\"} to the reference to deliver the secret as a file on the runner: the variable then holds the file's absolute path (usable as file(var.x), or by a tool reading a path from an env var). name defaults to the key; a relative name lands under /var/run/terrapod/files/, a name starting ~/ in the runner's home (e.g. ~/.aws/credentials). Not allowed with structured=true (or its alias hcl=true). The file's content is exactly one of: the reference's field (add \"encoding\":\"base64\" inside file to decode it to UTF-8 text); \"template\" inside file with no field, a logic-less template over the one read such as \"[default]\\naws_access_key_id = {{ access_key }}\\n\" (dotted names reach into maps; filters json, base64decode, trim, lines, indent N; _lease.ttl, _lease.renewable, _lease.expires_at when the secret has a lease; at most 16 KiB); or \"format\" inside file with no field, json or env for the whole secret, optionally narrowed by \"fields\":[...]"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_variable_set",
		Description: "Set a workspace variable — creates it if the key is new, updates it in place if it exists (an upsert keyed on `key`). " +
			"category defaults to terraform; set category=env for an environment variable, or git_http_auth/git_ssh_auth for private-git-module credentials (JSON value, always sensitive — see the module-auth docs). Set structured=true for non-string values (lists/objects/numbers); `hcl` is its deprecated alias. " +
			"category=terraform is the right answer on EVERY engine — it is the engine's own parameter channel, not Terraform's alone. On a Pulumi workspace it becomes stack config: the key passes through verbatim (including a namespaced form such as aws:region), sensitive=true makes it a real Pulumi secret so the engine renders it as [secret] in previews and state, and structured=true sets a nested value rather than a literal dotted key. It overrides a committed Pulumi.<stack>.yaml key of the same name and leaves the rest of that file alone. " +
			"Set value_source=vault to store a reference to an OpenBao (or HashiCorp Vault) secret instead of a literal, so the secret stays in OpenBao/Vault and is read per run — an unresolvable reference fails the run rather than delivering nothing. " +
			"A reference with a \"file\" object delivers the secret as a file and the variable holds its path, for providers and tools that only read credentials from a file. Returns the variable.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in variableSetIn) (*mcp.CallToolResult, *terrapod.Variable, error) {
		if in.WorkspaceID == "" || in.Key == "" {
			return errText("workspace_id and key are required"), nil, nil
		}
		category := in.Category
		if category == "" {
			category = "terraform"
		}
		// Upsert: look up (category, key); update if present, else create. A
		// NotFound on the lookup is the create path, not an error.
		//
		// The category is part of the identity (#1898). Looking up by key alone
		// found a variable in ANY category and then PATCHed it with this
		// category -- so setting category=env on a workspace that already had a
		// terraform variable of the same key silently re-categorised the
		// terraform one instead of creating the env one.
		existing, err := c.GetVariableByKey(ctx, in.WorkspaceID, category, in.Key)
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
		Category    string `json:"category,omitempty" jsonschema:"which category to delete the key from (terraform, env, git_http_auth, git_ssh_auth; native is accepted for terraform). Optional: needed only when the same key exists in more than one category, which is refused rather than guessed"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_variable_delete",
		Description: "Delete a workspace variable by key. Irreversible (the value, if not sensitive, is gone) and it changes what the next run sees — confirm with the user. A variable is identified by category and key together, so if the key exists in more than one category the delete is refused and the categories listed; pass category to choose.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in variableDeleteIn) (*mcp.CallToolResult, *deleteOut, error) {
		if in.WorkspaceID == "" || in.Key == "" {
			return errText("workspace_id and key are required"), nil, nil
		}
		// A variable is identified by (category, key), so a bare key can name
		// more than one (#1898). This is a destructive tool, so an ambiguous key
		// is refused with the categories listed rather than resolved by a
		// default -- deleting the wrong variable is not recoverable, and an
		// agent that meant the other one has no way to tell afterwards.
		all, err := c.ListVariables(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), nil, nil
		}
		var matches []terrapod.Variable
		for _, v := range all {
			if v.Key == in.Key && (in.Category == "" || v.Category == in.Category) {
				matches = append(matches, v)
			}
		}
		switch {
		case len(matches) == 0:
			return errResult(&terrapod.NotFoundError{Resource: "variable", ID: in.Key}), nil, nil
		case len(matches) > 1:
			cats := make([]string, 0, len(matches))
			for _, v := range matches {
				cats = append(cats, v.Category)
			}
			return errText(fmt.Sprintf(
				"%q names %d variables on this workspace, in categories: %s. "+
					"Pass category to say which one to delete.",
				in.Key, len(matches), strings.Join(cats, ", "))), nil, nil
		}
		existing := matches[0]
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
