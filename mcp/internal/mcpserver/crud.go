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
// ansibleCollectionInput is one declared collection on a workspace (#1931).
// Ansible's own requirements entry minus the fields Terrapod does not accept.
type ansibleCollectionInput struct {
	Name    string `json:"name" jsonschema:"galaxy coordinate, namespace.name, lowercase"`
	Version string `json:"version,omitempty" jsonschema:"version or range; omit for whatever resolves"`
}

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
		Name                        string                   `json:"name" jsonschema:"the workspace name (unique within the org)"`
		Engine                      string                   `json:"engine,omitempty" jsonschema:"the execution engine family: terraform (default) or another engine this deployment enables, such as pulumi. NOT execution_backend, which picks tofu vs terraform within the Terraform engine. Workspaces are created here, in the UI or with the Terraform provider — never by an engine's own CLI"`
		ExecutionMode               string                   `json:"execution_mode,omitempty" jsonschema:"local or agent (default: server default)"`
		ExecutionBackend            string                   `json:"execution_backend,omitempty" jsonschema:"which binary runs a Terraform-engine workspace: tofu or terraform. A choice WITHIN the Terraform engine — Pulumi has one binary, so this has no meaning on a pulumi workspace (default: server default)"`
		EngineVersion               string                   `json:"engine_version,omitempty" jsonschema:"version of the engine this workspace runs; partial like 1.15 (means 1.15.*), no HCL operators"`
		TerraformVersion            string                   `json:"terraform_version,omitempty" jsonschema:"the same version under its original name; prefer engine_version. Setting both to different values is rejected"`
		AnsibleVersion              string                   `json:"ansible_version,omitempty" jsonschema:"ansible-core LINE whose execution environment this workspace's configure operations run in, e.g. 2.21; an exact patch is accepted and names its own line; omit or empty to inherit the deployment default"`
		AnsibleExecutionEnvironment string                   `json:"ansible_execution_environment,omitempty" jsonschema:"label of one of the deployment's own execution environments; OVERRIDES ansible_version when set, so the line is then ignored; omit or empty to use the built-in image for the line"`
		AnsibleCollections          []ansibleCollectionInput `json:"ansible_collections,omitempty" jsonschema:"the ansible collections installed before a configure runs, declared on the platform. Each entry is {\"name\": \"namespace.name\"} with an optional \"version\" (a pin or a range, as in ansible's own requirements.yml; omit for whatever resolves). These are installed AFTER the configuration's own requirements.yml and with --force, so an entry here OVERRIDES the repository's build of that collection and leaves every other one standing. Collections come from this deployment's own Galaxy surface -- its published ones and a pull-through proxy of upstream -- so source and type are deliberately not settable. ROLES are a different mechanism and are not declared here: they come from git, using the workspace's git credentials, declared in the configuration's own requirements.yml"`
		AutoApply                   *bool                    `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans (default false)"`
		AutoApplyMode               *string                  `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create (only plans that add resources), create_update (also in-place updates). create and create_update never auto-apply a destroy or replace. Set this OR auto_apply, not both."`
		AgentPoolID                 string                   `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...) for agent execution mode; assigns exactly one pool. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs                []string                 `json:"agent_pool_ids,omitempty" jsonschema:"agent pools this workspace may run on (apool-...). Flat set: a run is offered to every pool at once and whichever has a live runner claims it first, so losing one does not stop the workspace. Mutually exclusive with agent_pool_id"`
		WorkingDirectory            string                   `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		OIDCAudiences               map[string][]string      `json:"oidc_audiences,omitempty" jsonschema:"the per-provider audiences this workspace's run identity tokens are minted with -- its cloud identity opt-in (#1901). Omit it (the default) and the workspace overrides nothing, inheriting the deployment's catalogue; a deployment that configures no audiences mints nothing and its runs authenticate with the agent pool's own identity, exactly as before. A MAP keyed on the provider configuration a token is for, never a flat list: the bare provider type exactly as a provider block writes it (aws, vault), or type.alias for one aliased configuration (aws.west). The alias is part of the KEY -- do not split on the dot and do not fold aws.west into aws; they are separate targets, and the runner asks for whichever its configuration actually uses. Each value is ALWAYS a list even for a single entry, because a federation target's audience is one value and a list of several is a deliberate these-are-interchangeable statement; the runner mints one token per target carrying only that target's audiences. An audience is an opaque string the federation target itself names -- whatever your cloud's or secret store's trust configuration expects -- and Terrapod stores it verbatim; nothing here is specific to any one cloud, because Terrapod only mints an OIDC JWT and the runner writes it to a file. What you pass is this workspace's OVERRIDE, merged per key OVER the deployment's own catalogue, so naming one target does not restate the rest. The read side differs and this is the trap: a workspace reports its oidc-audiences as the MERGED view, so writing back what you read would promote every inherited entry into an override. A key whose list is EMPTY is refused (422) -- express no-audiences-for-this-target by leaving the key out, which falls back to the deployment's value, because an empty list is indistinguishable from a typo. A token audienced for two targets is replayable between them, so give a target only its own audiences. Setting this does NOT by itself grant anything: the federation target's own trust policy decides what a token bearing these audiences may do."`
		VCSConnectionID             string                   `json:"vcs_connection_id,omitempty" jsonschema:"VCS connection id to wire this workspace to a repo"`
		VCSRepoURL                  string                   `json:"vcs_repo_url,omitempty" jsonschema:"git repo URL (requires vcs_connection_id)"`
		VCSBranch                   string                   `json:"vcs_branch,omitempty" jsonschema:"tracked branch (empty = repo default)"`
		AllowForkPRPlans            *bool                    `json:"allow_fork_pr_plans,omitempty" jsonschema:"allow a pull request opened from a FORK to get a speculative plan. **Off by default**, which is the safe setting: such a plan runs the fork author's code with this workspace's full credential set (env variables, secret-manager values, git credentials, the runner's cloud identity), and that author has no write access and cannot merge, so the plan is the only path by which their code reaches those credentials. Pull requests from branches in the repository itself always plan and are unaffected. Set it true only on a workspace that holds nothing worth taking, and note that an autodiscovery rule carries its own value for the workspaces it creates (GHSA-gp5w-76rw-c452)"`
		OwnerEmail                  string                   `json:"owner_email,omitempty" jsonschema:"workspace owner email (defaults to the caller)"`
		Labels                      map[string]string        `json:"labels,omitempty" jsonschema:"key/value labels for RBAC + filtering (reserved keys rejected)"`
		PulumiBindPlan              *bool                    `json:"pulumi_bind_plan,omitempty" jsonschema:"Pulumi workspaces only: bind the update to the approved preview (preview --save-plan then up --plan). Off by default; setting it true is rejected on any other engine"`
		// Which operations the new workspace has (#1966). Both are *bool so
		// unset stays distinguishable from false: a plain bool would make
		// every caller that left the field alone send false, and on
		// PlanApplyEnabled -- whose default is TRUE -- that would disable
		// plan/apply on the workspaces least expecting it.
		PlanApplyEnabled *bool `json:"plan_apply_enabled,omitempty" jsonschema:"whether this workspace has a PLAN/APPLY operation -- the terraform, tofu or pulumi lifecycle. DEFAULTS TO TRUE, so omit it for an ordinary infrastructure workspace. Pass false only for a configure-only workspace, and pass configure_enabled=true in the same call when you do: both operations off is refused (422), because a workspace that can do nothing is not a state anyone wants -- lock the workspace instead, which parks it reversibly. Independent of configure_enabled, so a workspace may have both"`
		ConfigureEnabled *bool `json:"configure_enabled,omitempty" jsonschema:"whether this workspace has an ansible CONFIGURE operation. DEFAULTS TO FALSE, so the operation is opt-in and this is what turns it on -- setting config_directory alone does NOT (#1966), and a workspace with configure_enabled false has no configure operation and no ansible inventory however its roots are set. With it true, config_directory says WHERE the operation runs and inventory_paths says what it targets. Independent of plan_apply_enabled, so a workspace may have both; both off is refused (422)"`
		// The ansible configure operation's roots and run defaults (#1929).
		// ConfigDirectory is a plain string: it is an ordinary path whose ""
		// means the configuration version's root, so on CREATE "" and omitted
		// mean the same thing and there is no third state left to express
		// (#1966). The declaration lives in ConfigureEnabled above.
		ConfigDirectory          string   `json:"config_directory,omitempty" jsonschema:"the directory the ansible CONFIGURE operation runs in, relative to the configuration version's root: its fetch scope and the working directory both the inventory resolver and the playbook executor run from. An ordinary path, exactly like working_directory -- empty or omitted means the configuration version's ROOT, and a subdirectory means rooted there. It does NOT declare whether the workspace HAS a configure operation: configure_enabled does (#1966), so setting this alone turns nothing on, and leaving it empty on a configure-enabled workspace is the ordinary way to say \"at the root\" rather than a missing value. Distinct from working_directory, which is the terraform/pulumi root; one repository can have both, at different directories"`
		InventoryPaths           []string `json:"inventory_paths,omitempty" jsonschema:"the ansible inventory sources, each a FILE or a DIRECTORY relative to the repository root. ORDERED -- the order IS the precedence ansible applies, so this is a list and not a set, and reordering it changes which value wins. Name the files or directories you mean rather than a parent: -i over a directory holding both production and staging MERGES them into one inventory, which is how a staging workspace ends up carrying production hosts. Pass an empty list to remove every source"`
		DefaultPlaybook          string   `json:"default_playbook,omitempty" jsonschema:"the playbook a configure run executes unless the run names another, the way vcs_ref overrides vcs_branch on a run. Relative to config_directory. Empty means the workspace has no default and every configure run must name its playbook"`
		DefaultLimit             string   `json:"default_limit,omitempty" jsonschema:"the default --limit for a configure run, overridable per run. Empty means the whole inventory, which is ansible's own default. This narrows which HOSTS a run touches and is the configure operation's equivalent of -target on a plan"`
		InventoryIncludePlatform *bool    `json:"inventory_include_platform,omitempty" jsonschema:"whether the inventory rows declared through Terrapod (the terrapod_inventory_* tools) contribute to the resolved inventory. DEFAULTS TO TRUE, so passing false is a real change: it resolves the git source in inventory_paths alone and the declared hosts and groups stop taking part"`
		InventoryIgnorePaths     []string `json:"inventory_ignore_paths,omitempty" jsonschema:"paths excluded from the inventory resolution. Pass an empty list to remove every one"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_create",
		Description: "Create a workspace. Only `name` is required; everything else falls back to the instance default. " +
			"For agent execution set execution_mode=agent + agent_pool_id; for VCS-driven runs set vcs_connection_id + vcs_repo_url. " +
			"For a Pulumi workspace set engine=pulumi, and pulumi_bind_plan to bind its update to the approved preview. " +
			"A workspace declares which OPERATIONS it has, as two independent booleans: plan_apply_enabled (the terraform/tofu/pulumi lifecycle, default TRUE) and configure_enabled (the ansible configure operation, default FALSE). " +
			"So an ansible CONFIGURE operation needs configure_enabled=true — config_directory alone does not turn one on; it only says where it runs — plus inventory_paths and the run defaults; there is no separate inventory-settings object, these are workspace settings. " +
			"Both operations off is refused (422). Returns the created workspace.",
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
			Name:                        in.Name,
			Engine:                      in.Engine,
			ExecutionMode:               in.ExecutionMode,
			ExecutionBackend:            in.ExecutionBackend,
			EngineVersion:               version,
			AnsibleVersion:              in.AnsibleVersion,
			AnsibleExecutionEnvironment: in.AnsibleExecutionEnvironment,
			AnsibleCollections:          toSDKCollections(in.AnsibleCollections),
			AutoApply:                   in.AutoApply,
			AutoApplyMode:               in.AutoApplyMode,
			AgentPoolID:                 in.AgentPoolID,
			AgentPoolIDs:                in.AgentPoolIDs,
			WorkingDirectory:            in.WorkingDirectory,
			VCSConnectionID:             in.VCSConnectionID,
			VCSRepoURL:                  in.VCSRepoURL,
			VCSBranch:                   in.VCSBranch,
			OIDCAudiences:               in.OIDCAudiences,
			OwnerEmail:                  in.OwnerEmail,
			Labels:                      in.Labels,
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
			// #1966's two. Pointers straight through, so an omitted field
			// leaves the server's own default -- TRUE for plan/apply and
			// FALSE for configure -- rather than asserting a value. Both
			// false is refused by the server against the resulting row; not
			// re-checked here, for the reason PulumiBindPlan gives above.
			PlanApplyEnabled: in.PlanApplyEnabled,
			ConfigureEnabled: in.ConfigureEnabled,
			// #1929's six. ConfigDirectory is an ordinary path now, so "" and
			// omitted mean the same thing on create and a plain string is the
			// honest type; the SDK drops an empty one, which costs nothing
			// because "" is also the server's default (#1966).
			ConfigDirectory:          in.ConfigDirectory,
			InventoryPaths:           in.InventoryPaths,
			DefaultPlaybook:          in.DefaultPlaybook,
			DefaultLimit:             in.DefaultLimit,
			InventoryIncludePlatform: in.InventoryIncludePlatform,
			InventoryIgnorePaths:     in.InventoryIgnorePaths,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, ws, nil
	})

	// ── terrapod_workspace_update ────────────────────────────────────
	type workspaceUpdateIn struct {
		WorkspaceID                 string                   `json:"workspace_id" jsonschema:"the workspace id (ws-...) to update"`
		Name                        string                   `json:"name,omitempty" jsonschema:"rename the workspace (empty = leave)"`
		ExecutionMode               string                   `json:"execution_mode,omitempty" jsonschema:"local or agent"`
		ExecutionBackend            string                   `json:"execution_backend,omitempty" jsonschema:"which binary runs a Terraform-engine workspace: tofu or terraform. A choice WITHIN the Terraform engine — Pulumi has one binary, so this has no meaning on a pulumi workspace"`
		EngineVersion               string                   `json:"engine_version,omitempty" jsonschema:"version of the engine this workspace runs; partial like 1.15 (means 1.15.*)"`
		TerraformVersion            string                   `json:"terraform_version,omitempty" jsonschema:"the same version under its original name; prefer engine_version. Setting both to different values is rejected"`
		AnsibleVersion              string                   `json:"ansible_version,omitempty" jsonschema:"ansible-core LINE whose execution environment this workspace's configure operations run in, e.g. 2.21; an exact patch is accepted and names its own line; omit or empty to inherit the deployment default"`
		AnsibleExecutionEnvironment string                   `json:"ansible_execution_environment,omitempty" jsonschema:"label of one of the deployment's own execution environments; OVERRIDES ansible_version when set, so the line is then ignored; omit or empty to use the built-in image for the line"`
		AnsibleCollections          []ansibleCollectionInput `json:"ansible_collections,omitempty" jsonschema:"the ansible collections installed before a configure runs, declared on the platform. Each entry is {\"name\": \"namespace.name\"} with an optional \"version\" (a pin or a range, as in ansible's own requirements.yml; omit for whatever resolves). These are installed AFTER the configuration's own requirements.yml and with --force, so an entry here OVERRIDES the repository's build of that collection and leaves every other one standing. Collections come from this deployment's own Galaxy surface -- its published ones and a pull-through proxy of upstream -- so source and type are deliberately not settable. ROLES are a different mechanism and are not declared here: they come from git, using the workspace's git credentials, declared in the configuration's own requirements.yml. Omitting the field leaves the stored list alone; pass an empty list to clear it"`
		AutoApply                   *bool                    `json:"auto_apply,omitempty" jsonschema:"auto-apply successful plans"`
		AutoApplyMode               *string                  `json:"auto_apply_mode,omitempty" jsonschema:"conditional auto-apply: never, always, create, create_update. Set this OR auto_apply, not both."`
		AgentPoolID                 string                   `json:"agent_pool_id,omitempty" jsonschema:"agent pool id (apool-...); assigns exactly one pool, REPLACING any existing set. Mutually exclusive with agent_pool_ids"`
		AgentPoolIDs                []string                 `json:"agent_pool_ids,omitempty" jsonschema:"replace the workspace''s agent-pool set (apool-...). Flat set — every pool is equally eligible to claim a run. Mutually exclusive with agent_pool_id"`
		WorkingDirectory            string                   `json:"working_directory,omitempty" jsonschema:"subdirectory within the repo"`
		Labels                      map[string]string        `json:"labels,omitempty" jsonschema:"replace the label set (reserved keys rejected)"`
		PulumiBindPlan              *bool                    `json:"pulumi_bind_plan,omitempty" jsonschema:"Pulumi workspaces only: bind the update to the approved preview (preview --save-plan then up --plan). Off by default; rejected on any other engine"`
		AllowForkPRPlans            *bool                    `json:"allow_fork_pr_plans,omitempty" jsonschema:"allow a pull request opened from a FORK to get a speculative plan. **Off by default**, which is the safe setting: such a plan runs the fork author's code with this workspace's full credential set (env variables, secret-manager values, git credentials, the runner's cloud identity), and that author has no write access and cannot merge, so the plan is the only path by which their code reaches those credentials. Pull requests from branches in the repository itself always plan and are unaffected. Set it true only on a workspace that holds nothing worth taking, and note that an autodiscovery rule carries its own value for the workspaces it creates (GHSA-gp5w-76rw-c452)"`
		OIDCAudiences               map[string][]string      `json:"oidc_audiences,omitempty" jsonschema:"replace this workspace's per-provider audience overrides for its run identity tokens -- its cloud identity opt-in (#1901). Omitting the field leaves the existing overrides alone; pass an EMPTY object to clear every one of them, which drops the workspace back to inheriting the deployment's catalogue wholesale. A MAP keyed on the provider configuration a token is for, never a flat list: the bare provider type exactly as a provider block writes it (aws, vault), or type.alias for one aliased configuration (aws.west). The alias is part of the KEY -- do not split on the dot and do not fold aws.west into aws; they are separate targets, and the runner asks for whichever its configuration actually uses. Each value is ALWAYS a list even for a single entry, because a federation target's audience is one value and a list of several is a deliberate these-are-interchangeable statement; the runner mints one token per target carrying only that target's audiences. An audience is an opaque string the federation target itself names -- whatever your cloud's or secret store's trust configuration expects -- and Terrapod stores it verbatim; nothing here is specific to any one cloud, because Terrapod only mints an OIDC JWT and the runner writes it to a file. What you pass is this workspace's OVERRIDE, merged per key OVER the deployment's own catalogue, so naming one target does not restate the rest. The read side differs and this is the trap: a workspace reports its oidc-audiences as the MERGED view, so writing back what you read would promote every inherited entry into an override. A key whose list is EMPTY is refused (422) -- express no-audiences-for-this-target by leaving the key out, which falls back to the deployment's value, because an empty list is indistinguishable from a typo. A token audienced for two targets is replayable between them, so give a target only its own audiences. Setting this does NOT by itself grant anything: the federation target's own trust policy decides what a token bearing these audiences may do."`
		// Which operations the workspace has (#1966). The pair REPLACED the
		// old `clear_config_directory` flag: turning the configure operation
		// off is `configure_enabled: false`, and it deliberately leaves
		// config_directory where it is so re-enabling later resumes in the
		// same directory.
		PlanApplyEnabled *bool `json:"plan_apply_enabled,omitempty" jsonschema:"turn this workspace's PLAN/APPLY operation -- the terraform, tofu or pulumi lifecycle -- on or off. Omitting the field leaves the stored value alone. Turning it off is refused (422) unless the workspace has, or this same call gives it, a configure operation: the refusal is checked against the MERGED row, so passing plan_apply_enabled=false alone on an ordinary workspace fails rather than leaving it able to do nothing. Lock the workspace instead to park it reversibly. Independent of configure_enabled"`
		ConfigureEnabled *bool `json:"configure_enabled,omitempty" jsonschema:"turn this workspace's ansible CONFIGURE operation on or off. Omitting the field leaves the stored value alone. This is the ONLY way to turn the operation off (#1966), and it deliberately does NOT move config_directory, so a later configure_enabled=true resumes in the same directory. Turning it on does not need config_directory: empty means the configuration version's root. Turning it off is refused (422) if the workspace has no plan/apply operation either, checked against the merged row. Independent of plan_apply_enabled"`
		// The ansible configure operation's roots and run defaults (#1929).
		// ConfigDirectory stays a POINTER here for a different reason from
		// the booleans above and from the create tool's plain string: a PATCH
		// has to tell "leave the stored root alone" from "move it to the
		// configuration version's root", and on a workspace rooted at
		// `ansible` those are different requests. It no longer carries a
		// third state -- turning the operation off is ConfigureEnabled.
		ConfigDirectory          *string  `json:"config_directory,omitempty" jsonschema:"the directory the ansible CONFIGURE operation runs in, relative to the configuration version's root: its fetch scope and the working directory both the inventory resolver and the playbook executor run from. An ordinary path, exactly like working_directory -- empty means the configuration version's ROOT and a subdirectory means rooted there. It does NOT declare whether the workspace HAS a configure operation: configure_enabled does (#1966), so this cannot turn one on or off. On an update, OMITTING it leaves the stored value alone and passing an EMPTY STRING moves the operation to the configuration version's root, which is why this is the one ansible root that still distinguishes unset from empty. Distinct from working_directory, which is the terraform/pulumi root; one repository can have both, at different directories"`
		InventoryPaths           []string `json:"inventory_paths,omitempty" jsonschema:"the ansible inventory sources, each a FILE or a DIRECTORY relative to the repository root. ORDERED -- the order IS the precedence ansible applies, so this is a list and not a set, and reordering it changes which value wins. Name the files or directories you mean rather than a parent: -i over a directory holding both production and staging MERGES them into one inventory, which is how a staging workspace ends up carrying production hosts. Pass an empty list to remove every source. Omitting the field leaves the stored list alone"`
		DefaultPlaybook          *string  `json:"default_playbook,omitempty" jsonschema:"the playbook a configure run executes unless the run names another, the way vcs_ref overrides vcs_branch on a run. Relative to config_directory. Empty means the workspace has no default and every configure run must name its playbook. Pass an empty string to stop defaulting to a playbook"`
		DefaultLimit             *string  `json:"default_limit,omitempty" jsonschema:"the default --limit for a configure run, overridable per run. Empty means the whole inventory, which is ansible's own default. This narrows which HOSTS a run touches and is the configure operation's equivalent of -target on a plan. Pass an empty string to widen a run back to the whole inventory"`
		InventoryIncludePlatform *bool    `json:"inventory_include_platform,omitempty" jsonschema:"whether the inventory rows declared through Terrapod (the terrapod_inventory_* tools) contribute to the resolved inventory. DEFAULTS TO TRUE, so passing false is a real change: it resolves the git source in inventory_paths alone and the declared hosts and groups stop taking part"`
		InventoryIgnorePaths     []string `json:"inventory_ignore_paths,omitempty" jsonschema:"paths excluded from the inventory resolution. Pass an empty list to remove every one. Omitting the field leaves the stored list alone"`
		// Resource sizing (#2154). Absent from this tool until #2154, so an
		// agent could read a workspace's sizing with terrapod_workspace_get and
		// not change it. That asymmetry is now closed for everything except
		// the four #2174 reserves as judgements, named in the block below --
		// a count is not restated here, because a number in prose has no gate
		// behind it and this comment carried a stale one.
		//
		// The two PAIRS are deliberately separate, because the two containers
		// of a configure Job do different work: the orchestrator runs tofu
		// while the execution environment runs ansible against however many
		// hosts the inventory resolves to. One pair cannot size both.
		ResourceCPU      string `json:"resource_cpu,omitempty" jsonschema:"CPU request for this workspace's runner Jobs, as a Kubernetes quantity: a whole number of CPUs (1, 4) or millicores (500m). The LIMIT is twice this, computed automatically. Decimals (0.5) and decimal SI suffixes are refused (422), because the limit is 2x the request and those forms cannot be doubled -- accepting one would silently leave the limit equal to the request. Omit to leave the stored value alone"`
		ResourceMemory   string `json:"resource_memory,omitempty" jsonschema:"memory request for this workspace's runner Jobs, as a Kubernetes quantity in binary bytes (256Mi, 2Gi). Limit is 2x. Same refusals as resource_cpu"`
		EEResourceCPU    string `json:"ee_resource_cpu,omitempty" jsonschema:"CPU request for the Ansible EXECUTION ENVIRONMENT -- the sidecar container a configure run's playbooks execute in -- not the orchestrator, which resource_cpu sizes. Same quantity rules and the same 2x limit. EMPTY means this workspace takes the deployment default rather than sizing the environment itself, so passing an empty string is how a workspace rejoins that default; omitting the field leaves the stored value alone"`
		EEResourceMemory string `json:"ee_resource_memory,omitempty" jsonschema:"memory request for the Ansible execution-environment sidecar. Limit is 2x. Empty takes the deployment default. See ee_resource_cpu"`
		// The AI overrides (#2174). `ai_policy_mode` is here because the parity
		// gate demands it by NAME and refuses to let it be excused: this file's
		// own history is that attribute shipping "on three surfaces out of
		// four", and MCP was the fourth. Its two siblings come with it because
		// half a feature on a tool surface is worse than none -- an agent that
		// can set the gate but not the summary has to explain the asymmetry.
		//
		// All three are three-state strings rather than booleans: a workspace
		// may defer to the deployment rather than choosing, which is what
		// `inherit` means and what an empty value leaves alone.
		AIPolicyMode  string `json:"ai_policy_mode,omitempty" jsonschema:"this workspace's override for the AI POLICY GATE -- whether a run's plan is reviewed by a model before it may apply: enabled, disabled or inherit. inherit defers to the deployment setting, which is the default and is NOT the same as disabled. Omit to leave the stored value alone. Distinct from ai_summary_mode: the gate can BLOCK a run, the summary only describes one"`
		AISummaryMode string `json:"ai_summary_mode,omitempty" jsonschema:"this workspace's override for AI PLAN SUMMARIES -- prose describing what a plan will do, which blocks nothing: enabled, disabled or inherit. inherit defers to the deployment setting. Omit to leave the stored value alone"`
		// A POINTER, unlike the two above, and the SDK is right to make it one:
		// the modes are three-state enums where "" means "leave alone", while
		// this is free prose where "" is a real value meaning "clear what is
		// stored". A plain string could not express the difference.
		AISummaryContext *string `json:"ai_summary_context,omitempty" jsonschema:"workspace-specific facts given to the model on top of the deployment's own context -- what this estate is for, naming conventions, which resources are load-bearing. Prose, not configuration: it changes what a summary SAYS, never what a run does. A hint rather than a repository, and capped at 4000 characters. Omitting the field leaves the stored value alone; pass an EMPTY STRING to clear it"`
		// The fourteen #2174 called wire plumbing: nothing was decided against
		// them, MCP simply had no parity gate until #2175 and so drifted for as
		// long as that gate had existed. An agent could READ every one of these
		// with terrapod_workspace_get and change none, which is the asymmetry
		// worth closing -- a tool surface that reports a setting it cannot set
		// invites an agent to describe a change it is unable to make.
		//
		// FOUR are deliberately still absent and stay in the gate's debt ledger
		// with their own reason: debug_mode arms a pod that holds the run's
		// credentials and keeps it alive for inspection, auto_merge and
		// auto_merge_strategy merge pull requests, and
		// security_scan_enforcement is a governance gate. Each reaches outside
		// the workspace's own configuration, so whether an agent should hold it
		// is a decision to argue on #2174 rather than an omission to fix here.
		//
		// Pointers where the zero value is a real request (a false, a 0, an
		// empty channel name); plain strings and slices where "" and nil mean
		// "leave the stored value alone", matching the fields above.
		DriftDetectionEnabled         *bool    `json:"drift_detection_enabled,omitempty" jsonschema:"whether Terrapod plans this workspace on a schedule to detect drift -- infrastructure that no longer matches the recorded state. A drift run is plan-only and never applies. Omit to leave the stored value alone"`
		DriftDetectionIntervalSeconds *int64   `json:"drift_detection_interval_seconds,omitempty" jsonschema:"how often a drift check runs, in seconds; 86400 (daily) by default. Only meaningful with drift_detection_enabled true. Omit to leave the stored value alone"`
		DriftIgnoreRules              []string `json:"drift_ignore_rules,omitempty" jsonschema:"resource addresses or attribute paths whose drift is not reported -- for the perpetual phantom diffs a provider produces on write-only attributes. Pass an empty list to remove every rule; omitting the field leaves the stored list alone"`
		Parallelism                   int64    `json:"parallelism,omitempty" jsonschema:"the engine's -parallelism for this workspace's runs; 10 by default, which is the engine's own. Lower it for a provider that rate-limits; raising it rarely helps and can trip API quotas. Omit to leave the stored value alone"`
		PlanExpirySeconds             *int64   `json:"plan_expiry_seconds,omitempty" jsonschema:"how long an apply-capable plan may wait for confirmation before it is expired; unset means never, which is the default. It does NOT expire speculative plans. Omit to leave the stored value alone"`
		SecurityScanEngine            string   `json:"security_scan_engine,omitempty" jsonschema:"which scanner reviews a plan: checkov or trivy. Omit to leave the stored value alone"`
		SecurityScanSeverityThreshold string   `json:"security_scan_severity_threshold,omitempty" jsonschema:"the lowest severity a scan finding must reach to be reported: critical, high, medium or low. This is what gets REPORTED, not what blocks -- the enforcement level is a separate setting and is deliberately not on this tool. Omit to leave the stored value alone"`
		SecurityScanSkipRules         []string `json:"security_scan_skip_rules,omitempty" jsonschema:"scanner rule ids this workspace does not report, as the scanner itself names them (e.g. CKV_AWS_18). Pass an empty list to remove every skip; omitting the field leaves the stored list alone"`
		SlackChannel                  *string  `json:"slack_channel,omitempty" jsonschema:"the Slack channel this workspace's run notifications go to, overriding the deployment default. Pass an EMPTY STRING to drop back to the default; omitting the field leaves the stored value alone"`
		TerragruntEnabled             *bool    `json:"terragrunt_enabled,omitempty" jsonschema:"run this workspace through terragrunt rather than the engine binary directly. Omit to leave the stored value alone"`
		TerragruntVersion             string   `json:"terragrunt_version,omitempty" jsonschema:"which terragrunt version this workspace runs; only meaningful with terragrunt_enabled true. Omit to leave the stored value alone"`
		TriggerPrefixes               []string `json:"trigger_prefixes,omitempty" jsonschema:"the repository paths that make a push or pull request create a run here, each a directory prefix from the repository root. It REPLACES working_directory for the trigger decision and UNIONS with it for the fetch, so a monorepo workspace watching several directories names them all. A push touching no matching path creates no run at all. Pass an empty list to remove every prefix, which falls back to working_directory; omitting the field leaves the stored list alone"`
		VarFiles                      []string `json:"var_files,omitempty" jsonschema:".tfvars files from the repository, applied in order, relative to the working directory. Pass an empty list to remove every one; omitting the field leaves the stored list alone"`
		VCSWorkflow                   string   `json:"vcs_workflow,omitempty" jsonschema:"when a pull request's run applies: merge_then_apply (the default -- the PR gets a speculative plan, and the apply happens after it merges) or apply_then_merge (the PR's plan is applied BEFORE the merge, which holds the workspace lock while the PR is open and is what Atlantis does). Omit to leave the stored value alone"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_workspace_update",
		Description: "Update a workspace's settings. Only the fields you pass change; omitted fields are left alone. " +
			"This is a config change (the new settings apply on the workspace's next run) — it does not itself queue a run. " +
			"Which OPERATIONS the workspace has lives here as two independent booleans: plan_apply_enabled (the terraform/tofu/pulumi lifecycle) and configure_enabled (the ansible configure operation). configure_enabled=false is how you turn a configure operation off, and it leaves config_directory where it is so re-enabling resumes in the same directory; turning BOTH off is refused (422) against the resulting row. " +
			"The ansible configure operation's roots live here too (config_directory, inventory_paths, default_playbook, default_limit, inventory_include_platform, inventory_ignore_paths) — there is no separate inventory-settings object to configure.",
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
			Name:                        in.Name,
			ExecutionMode:               in.ExecutionMode,
			ExecutionBackend:            in.ExecutionBackend,
			EngineVersion:               version,
			AnsibleVersion:              in.AnsibleVersion,
			AnsibleExecutionEnvironment: in.AnsibleExecutionEnvironment,
			AnsibleCollections:          toSDKCollections(in.AnsibleCollections),
			AutoApply:                   in.AutoApply,
			AutoApplyMode:               in.AutoApplyMode,
			AgentPoolID:                 in.AgentPoolID,
			AgentPoolIDs:                in.AgentPoolIDs,
			WorkingDirectory:            in.WorkingDirectory,
			Labels:                      in.Labels,
			PulumiBindPlan:              in.PulumiBindPlan,
			OIDCAudiences:               in.OIDCAudiences,
			AllowForkPRPlans:            in.AllowForkPRPlans,
			// #1966's two. Pointers straight through, so a PATCH that
			// mentions neither sends neither and cannot disturb a stored
			// declaration. The both-false refusal is the server's, checked
			// against the merged row -- a copy here could only see the
			// payload, which is the half that cannot answer the question.
			PlanApplyEnabled: in.PlanApplyEnabled,
			ConfigureEnabled: in.ConfigureEnabled,
			// #1929's six. ConfigDirectory keeps its pointer so "leave the
			// stored root alone" stays distinguishable from "move it to the
			// configuration version's root"; a nil sends nothing, which is
			// what makes turning the operation off leave the path untouched.
			ConfigDirectory:          in.ConfigDirectory,
			InventoryPaths:           in.InventoryPaths,
			DefaultPlaybook:          in.DefaultPlaybook,
			DefaultLimit:             in.DefaultLimit,
			InventoryIncludePlatform: in.InventoryIncludePlatform,
			InventoryIgnorePaths:     in.InventoryIgnorePaths,
			// #2154's two pairs. Plain strings, so the SDK's own `!= ""` guards
			// decide what reaches the wire -- which means an empty value cannot
			// be SENT, and clearing an EE size back to the deployment default is
			// therefore not expressible here. That is the same limitation
			// `ansible_version` has had since #2010 and the sibling fields above
			// share; a pointer for these two alone would make the tool
			// inconsistent for a case nothing has asked for yet.
			ResourceCPU:      in.ResourceCPU,
			ResourceMemory:   in.ResourceMemory,
			EEResourceCPU:    in.EEResourceCPU,
			EEResourceMemory: in.EEResourceMemory,
			AIPolicyMode:     in.AIPolicyMode,
			AISummaryMode:    in.AISummaryMode,
			AISummaryContext: in.AISummaryContext,
			// #2174's fourteen. The SDK already carried every one of these --
			// the gap was only ever this tool -- so each is a straight pass
			// through, typed to the SDK's own int64 so nothing needs converting.
			DriftDetectionEnabled:         in.DriftDetectionEnabled,
			DriftDetectionIntervalSeconds: in.DriftDetectionIntervalSeconds,
			DriftIgnoreRules:              in.DriftIgnoreRules,
			Parallelism:                   in.Parallelism,
			PlanExpirySeconds:             in.PlanExpirySeconds,
			SecurityScanEngine:            in.SecurityScanEngine,
			SecurityScanSeverityThreshold: in.SecurityScanSeverityThreshold,
			SecurityScanSkipRules:         in.SecurityScanSkipRules,
			SlackChannel:                  in.SlackChannel,
			TerragruntEnabled:             in.TerragruntEnabled,
			TerragruntVersion:             in.TerragruntVersion,
			TriggerPrefixes:               in.TriggerPrefixes,
			VarFiles:                      in.VarFiles,
			VCSWorkflow:                   in.VCSWorkflow,
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
		Category    string `json:"category,omitempty" jsonschema:"terraform, env, git_http_auth, git_ssh_auth, ansible_machine_auth or ansible_vault_auth (default terraform); native is accepted as an equivalent name for terraform. terraform is the engine's own parameter channel -- Terraform input variables, Pulumi stack config, Ansible extra vars -- one category, delivered by whichever engine the workspace runs; Terrapod's own API calls it native, and this tool reads the compatibility surface, which calls it terraform. The other four are credential categories: the value is a JSON envelope rather than a literal, all four are always sensitive whatever you ask for, and none accepts an OpenBao/Vault value source. In the two ansible_* categories the key is the credential's NAME. For ansible_machine_auth the INVENTORY says where it is used, with a ${terrapod:<name>.<field>} placeholder the runner substitutes while rendering, so it can fill any connection variable; for ansible_vault_auth the name is also the vault ID label"`
		Structured  *bool  `json:"structured,omitempty" jsonschema:"the value is a typed expression rather than a plain string (lists/objects/numbers/bools); default false"`
		HCL         *bool  `json:"hcl,omitempty" jsonschema:"deprecated alias for structured; both are the same flag"`
		Sensitive   *bool  `json:"sensitive,omitempty" jsonschema:"mark sensitive — masked at rest and in responses; default false"`
		ApplyOnly   *bool  `json:"apply_only,omitempty" jsonschema:"withhold this variable from a plan and deliver it only to an apply; default false. A plan executes the configuration's code, so a credential only the apply needs should not be in a plan's environment. For a different value per phase, set TWO variables with the same key — one ordinary, one apply_only — and the apply-only one wins on an apply"`
		Description string `json:"description,omitempty" jsonschema:"optional human description"`
		ValueSource string `json:"value_source,omitempty" jsonschema:"static (default — value is the literal) or vault, where value is a JSON reference {\"mount\":…,\"path\":…,\"field\":…} that Terrapod reads from OpenBao (or HashiCorp Vault) at run time; a vault-sourced variable is always sensitive and the secret is never stored in Terrapod. Add \"file\":{\"name\":\"gcp/adc.json\"} to the reference to deliver the secret as a file on the runner: the variable then holds the file's absolute path (usable as file(var.x), or by a tool reading a path from an env var). name defaults to the key; a relative name lands under /var/run/terrapod/files/, a name starting ~/ in the runner's home (e.g. ~/.aws/credentials). Not allowed with structured=true (or its alias hcl=true). The file's content is exactly one of: the reference's field (add \"encoding\":\"base64\" inside file to decode it to UTF-8 text); \"template\" inside file with no field, a logic-less template over the one read such as \"[default]\\naws_access_key_id = {{ access_key }}\\n\" (dotted names reach into maps; filters json, base64decode, trim, lines, indent N; _lease.ttl, _lease.renewable, _lease.expires_at when the secret has a lease; at most 16 KiB); or \"format\" inside file with no field, json or env for the whole secret, optionally narrowed by \"fields\":[...]"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_variable_set",
		Description: "Set a workspace variable — creates it if the key is new, updates it in place if it exists (an upsert keyed on `key`). " +
			"category defaults to terraform; set category=env for an environment variable, git_http_auth/git_ssh_auth for private-git-module credentials, or ansible_machine_auth/ansible_vault_auth for the credentials a configure connects and decrypts with. All four credential categories take a JSON envelope, are always sensitive, and use `key` as the credential's scope rather than as a name. Set structured=true for non-string values (lists/objects/numbers); `hcl` is its deprecated alias. " +
			"category=terraform is the right answer on EVERY engine — it is the engine's own parameter channel, not Terraform's alone. On a Pulumi workspace it becomes stack config: the key passes through verbatim (including a namespaced form such as aws:region), sensitive=true makes it a real Pulumi secret so the engine renders it as [secret] in previews and state, and structured=true sets a nested value rather than a literal dotted key. It overrides a committed Pulumi.<stack>.yaml key of the same name and leaves the rest of that file alone. " +
			"Set value_source=vault to store a reference to an OpenBao (or HashiCorp Vault) secret instead of a literal, so the secret stays in OpenBao/Vault and is read per run — an unresolvable reference fails the run rather than delivering nothing. " +
			"A reference with a \"file\" object delivers the secret as a file and the variable holds its path, for providers and tools that only read credentials from a file. " +
			"Set apply_only=true to withhold the variable from a plan and deliver it only to an apply, so a write-capable credential is never in the environment of a plan that runs a pull request's code. Returns the variable.",
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
				ApplyOnly:   in.ApplyOnly,
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
			ApplyOnly:   boolOrFalse(in.ApplyOnly),
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
		Category    string `json:"category,omitempty" jsonschema:"which category to delete the key from (terraform, env, git_http_auth, git_ssh_auth, ansible_machine_auth, ansible_vault_auth; native is accepted for terraform). Optional: needed only when the same key exists in more than one category, which is refused rather than guessed"`
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

// toSDKCollections converts the tool input to the SDK shape, nil for absent so
// the key is omitted and the stored list is left alone.
func toSDKCollections(in []ansibleCollectionInput) []terrapod.AnsibleCollection {
	if in == nil {
		return nil
	}
	out := make([]terrapod.AnsibleCollection, 0, len(in))
	for _, c := range in {
		out = append(out, terrapod.AnsibleCollection{Name: c.Name, Version: c.Version})
	}
	return out
}
