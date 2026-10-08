package mcpserver

import (
	"context"
	"errors"
	"strings"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerInventory adds the ansible inventory tools: the eight structures a
// workspace declares (#1968) and the resolution they feed (#1967).
//
// # Why the resolved read is the tool that earns its place
//
// Auto-configure is deliberately broad — the user's decision, in their words:
// "People operating at scale might prefer the blast radius to the manual
// processes." A change to a playbook, a role or an inventory source can
// therefore reconfigure everything that source reaches, with no human asked.
// That is the intended behaviour, so **visibility is the control rather than
// prevention**, and an agent being able to answer "what would this target"
// before anything runs is the safety surface this whole group exists to give
// it. Ask with a `limit`, show the host list, then act.
//
// # These tools WRITE, and the earlier argument against that was wrong
//
// This group used to be read-only, reasoning that a host written through the
// API is in no configuration's state and so collides with a later apply. It
// does collide — and the collision is a 409, after which the practitioner
// imports the row, which is how every other Terraform-managed thing here
// behaves. Declining to offer the write did not prevent the collision; it just
// made the API the only place to cause it.
//
// # There is no refresh, because a read is live
//
// Dynamic inventory was declined (#1970), so every source is static and
// `terrapod_inventory_resolved` resolves the rows to answer the call. There is
// no stale copy for a write tool to bring up to date.
func registerInventory(s *mcp.Server, c *terrapod.Client) {
	registerInventoryReads(s, c)
	registerInventoryWrites(s, c)
}

// ── Reads ────────────────────────────────────────────────────────────────────

func registerInventoryReads(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_inventory_settings ──────────────────────────────────
	type settingsIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...)"`
	}
	type settingsOut struct {
		// Configured is false when the workspace has no settings row, which is
		// the NORMAL default rather than a failure: it means no VCS source is
		// bound and the declared rows are the whole inventory. Reporting that
		// as a tool error would have an agent relay a problem where there is
		// none.
		Configured bool                        `json:"configured"`
		Settings   *terrapod.InventorySettings `json:"settings,omitempty"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_settings",
		Description: "Read a workspace's ansible inventory settings: whether its declared rows contribute (`include_platform`), and the git source merged with them. " +
			"`configured: false` is the normal default and means no git source is bound — the declared hosts and groups are the whole inventory. It is not an error. " +
			"This binding is SEPARATE from the workspace's terraform VCS binding: even in one repository the root directories differ, and a configure-only workspace has no terraform binding at all. " +
			"`working_directory` points at a DIRECTORY, which ansible reads as one source in lexical filename order — so one binding already carries arbitrarily many inventory files, and their ordering is the operator's business via filenames.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in settingsIn) (*mcp.CallToolResult, settingsOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), settingsOut{}, nil
		}
		settings, err := c.GetInventorySettings(ctx, in.WorkspaceID)
		if err != nil {
			var nf *terrapod.NotFoundError
			if errors.As(err, &nf) {
				return nil, settingsOut{Configured: false}, nil
			}
			return errResult(err), settingsOut{}, nil
		}
		return nil, settingsOut{Configured: true, Settings: settings}, nil
	})

	// ── terrapod_inventory_list ──────────────────────────────────────
	type listIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose inventory to list"`
	}
	type listOut struct {
		HostCount  int                       `json:"host_count"`
		GroupCount int                       `json:"group_count"`
		Hosts      []terrapod.InventoryHost  `json:"hosts"`
		Groups     []terrapod.InventoryGroup `json:"groups"`
		// Vars under `all` — ansible's group_vars/all. Parented on the
		// workspace, because `all` cannot BE a group: it is the rendered
		// document's own root key.
		VarsUnderAll []terrapod.InventoryVar `json:"vars_under_all"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_list",
		Description: "List a workspace's declared ansible inventory: every host, every group, and the variables under `all`. " +
			"Each host and group carries COUNTS of its variables and links rather than the rows — a listing must not grow with the whole inventory — so follow up with terrapod_inventory_detail on whichever one matters. " +
			"These are what the workspace declares. The INVENTORY is these merged with any git source (terrapod_inventory_settings), and terrapod_inventory_resolved is the merged answer. " +
			"Pages through every host and group, so this is the full declared set rather than one page. " +
			"Empty for a workspace that declares none — most workspaces manage infrastructure and no hosts.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in listIn) (*mcp.CallToolResult, listOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), listOut{}, nil
		}
		hosts, err := c.ListAllInventoryHosts(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), listOut{}, nil
		}
		groups, err := c.ListAllInventoryGroups(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), listOut{}, nil
		}
		vars, err := c.ListInventoryGlobalVars(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), listOut{}, nil
		}
		// A nil slice marshals to `null`, which fails the tool's derived output
		// schema ("want array") and costs the agent the whole listing rather
		// than reporting an empty set — and an empty set is the common answer
		// here, not an edge case.
		if hosts == nil {
			hosts = []terrapod.InventoryHost{}
		}
		if groups == nil {
			groups = []terrapod.InventoryGroup{}
		}
		if vars == nil {
			vars = []terrapod.InventoryVar{}
		}
		return nil, listOut{
			HostCount: len(hosts), GroupCount: len(groups),
			Hosts: hosts, Groups: groups, VarsUnderAll: vars,
		}, nil
	})

	// ── terrapod_inventory_detail ────────────────────────────────────
	type detailIn struct {
		ID string `json:"id" jsonschema:"a host id (invhost-...) or a group id (invgroup-...). The prefix says which, so there is no kind to pass"`
	}
	type detailOut struct {
		Host   *terrapod.InventoryHost       `json:"host,omitempty"`
		Group  *terrapod.InventoryGroup      `json:"group,omitempty"`
		Vars   []terrapod.InventoryVar       `json:"vars"`
		Groups []terrapod.InventoryHostGroup `json:"groups,omitempty"`
		Hosts  []terrapod.InventoryHostGroup `json:"hosts,omitempty"`
		// Children and Parents are only meaningful for a group. A group may
		// have several parents, so Parents is a list rather than a field.
		Children []terrapod.InventoryGroupChild `json:"children,omitempty"`
		Parents  []terrapod.InventoryGroupChild `json:"parents,omitempty"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_detail",
		Description: "Read one host or one group with its variables and its links — a host's groups, or a group's member hosts, child groups and parent groups. " +
			"Pass a host id or a group id; the typed prefix says which, so there is no kind argument to get wrong. " +
			"A variable's value is returned as `***` when `sensitive` is true: the server masks it in every response and never returns the stored value. Do not write that mask back as a value.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in detailIn) (*mcp.CallToolResult, detailOut, error) {
		switch {
		case in.ID == "":
			return errText("id is required: a host id (invhost-...) or a group id (invgroup-...)"), detailOut{}, nil
		case strings.HasPrefix(in.ID, "invhost-"):
			host, err := c.GetInventoryHost(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			vars, err := c.ListInventoryHostVars(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			links, err := c.ListInventoryHostMemberships(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			return nil, detailOut{
				Host: host, Vars: orEmptyVars(vars), Groups: orEmptyMemberships(links),
			}, nil
		case strings.HasPrefix(in.ID, "invgroup-"):
			group, err := c.GetInventoryGroup(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			vars, err := c.ListInventoryGroupVars(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			members, err := c.ListInventoryGroupMembers(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			children, err := c.ListInventoryGroupChildren(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			parents, err := c.ListInventoryGroupParents(ctx, in.ID)
			if err != nil {
				return errResult(err), detailOut{}, nil
			}
			return nil, detailOut{
				Group: group, Vars: orEmptyVars(vars), Hosts: orEmptyMemberships(members),
				Children: orEmptyNestings(children), Parents: orEmptyNestings(parents),
			}, nil
		default:
			return errText("id must be a host id (invhost-...) or a group id (invgroup-...), got " + in.ID), detailOut{}, nil
		}
	})

	// ── terrapod_inventory_resolved ──────────────────────────────────
	type resolvedIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose inventory to resolve"`
		Limit       string `json:"limit,omitempty" jsonschema:"an ansible --limit pattern to apply: host names, group names, globs, 'all', comma- or colon-separated terms, '!' to exclude, '&' to intersect, '~' for a regex. Omit it to resolve the whole inventory"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_resolved",
		Description: "Read what a workspace's inventory resolves to: every host with its merged variables, every group's direct membership, and the group nesting. " +
			"This is the target set a configure would run against, so it is what to show a user before anything runs. Pass `limit` to see what a `--limit` pattern would select — that is the authoritative answer to 'what would this target', and it is ansible's own expansion, so every term ansible supports works INCLUDING `~regex`. " +
			"`groups` is DIRECT membership only. Ansible does not flatten nesting into a group's host list, so a parent whose members all arrive through a child shows an EMPTY list of its own — do not read that as 'targets nothing'. `group_children` carries the structure, and asking with `limit` set to the group name gives its effective set, because a limit DOES expand through nesting. " +
			"`hosts` is EXHAUSTIVE — it includes a host with no variables, deliberately unlike ansible's own `_meta.hostvars`, which omits one entirely; enumerating a host set from ansible's shape loses hosts silently. " +
			"ANSIBLE produces this, not Terrapod: the declared rows are rendered to one document and `ansible-inventory --list` is run over that and the git source, so the precedence rules and the group DAG are ansible's. " +
			"It is LIVE and there is nothing to refresh. Every source is static, so Terrapod resolves the rows to answer this call — the hosts you read are the hosts as they are. There is deliberately no timestamp and no freshness field. " +
			"An error here is a real error and never an empty host set: a configure targeting too little is worse than no answer, so this fails closed.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in resolvedIn) (*mcp.CallToolResult, *terrapod.ResolvedInventory, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		var (
			resolved *terrapod.ResolvedInventory
			err      error
		)
		if in.Limit == "" {
			resolved, err = c.GetResolvedInventory(ctx, in.WorkspaceID)
		} else {
			resolved, err = c.GetResolvedInventoryWithLimit(ctx, in.WorkspaceID, in.Limit)
		}
		if err != nil {
			return errResult(err), nil, nil
		}
		// Empty maps rather than nil: a pattern that matches nothing is a real
		// and important answer, and `null` fails the derived output schema.
		if resolved.Hosts == nil {
			resolved.Hosts = map[string]map[string]any{}
		}
		if resolved.Groups == nil {
			resolved.Groups = map[string][]string{}
		}
		if resolved.GroupChildren == nil {
			resolved.GroupChildren = map[string][]string{}
		}
		return nil, resolved, nil
	})
}

// ── Writes ───────────────────────────────────────────────────────────────────

func registerInventoryWrites(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_inventory_settings_set ──────────────────────────────
	type settingsSetIn struct {
		WorkspaceID        string   `json:"workspace_id" jsonschema:"the workspace id (ws-...)"`
		IncludePlatform    *bool    `json:"include_platform,omitempty" jsonschema:"whether the declared rows contribute to the resolution; default true"`
		VCSConnectionID    string   `json:"vcs_connection_id,omitempty" jsonschema:"the VCS connection to fetch the git inventory source with. Pass an empty string with clear_vcs_connection to remove the binding"`
		RepoURL            string   `json:"repo_url,omitempty" jsonschema:"the repository holding the inventory"`
		Branch             string   `json:"branch,omitempty" jsonschema:"the branch to read; empty means the repository default"`
		WorkingDirectory   string   `json:"working_directory,omitempty" jsonschema:"the DIRECTORY within the repository that ansible reads as one source; empty means the root"`
		IgnorePaths        []string `json:"ignore_paths,omitempty" jsonschema:"paths within the source to skip"`
		ClearVCSConnection bool     `json:"clear_vcs_connection,omitempty" jsonschema:"remove the git binding entirely, leaving the declared rows as the whole inventory"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_settings_set",
		Description: "Create or change a workspace's ansible inventory settings. Only the fields you pass are changed; the rest are left alone. " +
			"`working_directory` is a DIRECTORY, not a file: ansible reads it as one source in lexical filename order, so one binding carries every inventory file in it. " +
			"A repository needs a `vcs_connection_id` to fetch it with, and setting one without the other is refused. Pass `clear_vcs_connection` to remove the git source and leave the declared rows as the whole inventory. " +
			"The git source is merged BENEATH the declared rows, so a variable declared here wins a conflict with the committed inventory — the committed file is the baseline and what the platform declares overrides it.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in settingsSetIn) (*mcp.CallToolResult, *terrapod.InventorySettings, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		req := terrapod.UpdateInventorySettingsRequest{
			IncludePlatform:    in.IncludePlatform,
			ClearVCSConnection: in.ClearVCSConnection,
		}
		if in.VCSConnectionID != "" {
			req.VCSConnectionID = &in.VCSConnectionID
		}
		if in.RepoURL != "" {
			req.RepoURL = &in.RepoURL
		}
		if in.Branch != "" {
			req.Branch = &in.Branch
		}
		if in.WorkingDirectory != "" {
			req.WorkingDirectory = &in.WorkingDirectory
		}
		if in.IgnorePaths != nil {
			req.IgnorePaths = &in.IgnorePaths
		}

		settings, err := c.UpdateInventorySettings(ctx, in.WorkspaceID, req)
		if err == nil {
			return nil, settings, nil
		}
		// The patch route answers 404 when there is no row to patch, so the
		// first call for a workspace has to create one. Falling back rather
		// than asking the agent to know which it is: an agent cannot tell
		// "configure this" from "reconfigure this" without a read it should
		// not need, and the two produce the same intent.
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			return errResult(err), nil, nil
		}
		put := terrapod.PutInventorySettingsRequest{
			IncludePlatform:  in.IncludePlatform == nil || *in.IncludePlatform,
			RepoURL:          in.RepoURL,
			Branch:           in.Branch,
			WorkingDirectory: in.WorkingDirectory,
			IgnorePaths:      in.IgnorePaths,
		}
		if !in.ClearVCSConnection {
			put.VCSConnectionID = in.VCSConnectionID
		}
		settings, err = c.PutInventorySettings(ctx, in.WorkspaceID, put)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, settings, nil
	})

	// ── terrapod_inventory_host_declare ──────────────────────────────
	type hostDeclareIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) to declare the host in"`
		Name        string `json:"name" jsonschema:"the inventory hostname (ansible's inventory_hostname). Names containing --limit's own operators and separators (comma, colon, '!', '&', '~') or whitespace are refused, because a host named with one of those cannot be targeted"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_host_declare",
		Description: "Declare a host in a workspace's ansible inventory. " +
			"`name` is the only field a host has: `ansible_host`, `ansible_user` and everything else are VARIABLES, set with terrapod_inventory_var_set. " +
			"A host of that name already in this workspace is a conflict rather than an update — if a configuration declares it, the practitioner imports the existing row instead of recreating it. " +
			"A host declared here is in no configuration's state, so a later terraform apply that declares the same name will collide the same way. Prefer editing the configuration where one manages this inventory.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in hostDeclareIn) (*mcp.CallToolResult, *terrapod.InventoryHost, error) {
		if in.WorkspaceID == "" || in.Name == "" {
			return errText("workspace_id and name are both required"), nil, nil
		}
		host, err := c.CreateInventoryHost(ctx, in.WorkspaceID, in.Name)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, host, nil
	})

	// ── terrapod_inventory_group_declare ─────────────────────────────
	type groupDeclareIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) to declare the group in"`
		Name        string `json:"name" jsonschema:"the group name. 'all' and 'ungrouped' are refused: ansible derives both, and 'all' is the rendered document's own root key"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_group_declare",
		Description: "Declare a group in a workspace's ansible inventory. " +
			"`all` and `ungrouped` are refused as names — ansible derives both. To set a variable for every host, use terrapod_inventory_var_set with `scope: all`, which is ansible's group_vars/all. " +
			"A group is empty until hosts are put in it with terrapod_inventory_link.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in groupDeclareIn) (*mcp.CallToolResult, *terrapod.InventoryGroup, error) {
		if in.WorkspaceID == "" || in.Name == "" {
			return errText("workspace_id and name are both required"), nil, nil
		}
		group, err := c.CreateInventoryGroup(ctx, in.WorkspaceID, in.Name)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, group, nil
	})

	// ── terrapod_inventory_link ──────────────────────────────────────
	type linkIn struct {
		GroupID      string `json:"group_id" jsonschema:"the group id (invgroup-...) to add to"`
		HostID       string `json:"host_id,omitempty" jsonschema:"a host id (invhost-...) to put in the group — a '[groupname]' host line"`
		ChildGroupID string `json:"child_group_id,omitempty" jsonschema:"a group id (invgroup-...) to nest inside the group — a '[groupname:children]' entry"`
	}
	type linkOut struct {
		Membership *terrapod.InventoryHostGroup  `json:"membership,omitempty"`
		Nesting    *terrapod.InventoryGroupChild `json:"nesting,omitempty"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_link",
		Description: "Put a host in a group, or nest one group inside another. Pass `group_id` with EITHER `host_id` (a membership) or `child_group_id` (a nesting), not both. " +
			"Both are many-to-many: a host belongs to as many groups as you like, and a group may have several parents. A cycle is refused, and so is nesting a group inside itself. " +
			"Both sides must be in the same workspace; the database refuses a link that spans two, so this cannot create one by mistake. " +
			"Each link is its own row with its own id, so a second concern can create one without owning either side — and remove it with terrapod_inventory_remove without touching the host or the group.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in linkIn) (*mcp.CallToolResult, linkOut, error) {
		switch {
		case in.GroupID == "":
			return errText("group_id is required"), linkOut{}, nil
		case in.HostID != "" && in.ChildGroupID != "":
			return errText("pass host_id or child_group_id, not both: one call makes one link"), linkOut{}, nil
		case in.HostID != "":
			link, err := c.AddHostToInventoryGroup(ctx, in.GroupID, in.HostID)
			if err != nil {
				return errResult(err), linkOut{}, nil
			}
			return nil, linkOut{Membership: link}, nil
		case in.ChildGroupID != "":
			link, err := c.AddInventoryGroupChild(ctx, in.GroupID, in.ChildGroupID)
			if err != nil {
				return errResult(err), linkOut{}, nil
			}
			return nil, linkOut{Nesting: link}, nil
		default:
			return errText("pass host_id to add a host, or child_group_id to nest a group"), linkOut{}, nil
		}
	})

	// ── terrapod_inventory_var_set ───────────────────────────────────
	type varSetIn struct {
		Key string `json:"key" jsonschema:"the variable name, e.g. ansible_host or http_port"`
		// Exactly one of these three says where the variable lives, which is
		// also what makes it unambiguous without a kind string to mistype.
		HostID      string `json:"host_id,omitempty" jsonschema:"set on one host — ansible's host_vars/<host>"`
		GroupID     string `json:"group_id,omitempty" jsonschema:"set on one group — ansible's group_vars/<group>"`
		WorkspaceID string `json:"workspace_id,omitempty" jsonschema:"set for every host — ansible's group_vars/all"`
		Value       string `json:"value" jsonschema:"the value. Text unless structured is true"`
		Structured  *bool  `json:"structured,omitempty" jsonschema:"read value as literal source for a list, number, bool or object rather than as text; default false"`
		Sensitive   *bool  `json:"sensitive,omitempty" jsonschema:"mask the value in every response; default false. Independent of app-layer encryption, which is a deployment-wide setting and covers every value or none"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_var_set",
		Description: "Set an ansible inventory variable, creating it or changing it. Pass exactly ONE of `host_id` (host_vars), `group_id` (group_vars) or `workspace_id` (group_vars/all) to say where it lives. " +
			"A list, number, bool or object is expressed by setting `structured` and sending its literal source in `value` — the same arrangement a structured workspace variable has. " +
			"`sensitive` masks the value in every response as `***`; it is a DISPLAY flag and it is NOT encryption. The two are independent: a column cannot be conditionally encrypted, so where the deployment has app-layer encryption enabled every value is enveloped whatever `sensitive` says, and where it does not (the default) the column is plaintext and the protection is the datastore's own at-rest encryption -- the same position as a workspace variable. So do not read `sensitive: true` as a statement about how the value is stored. " +
			"So reading a sensitive variable back gives the mask, and writing that mask back would store it as the secret. " +
			"Each variable is its own row with ONE writer, which is why variables are rows here rather than a map on their parent: a second concern can contribute one without owning the host or the group. " +
			"Precedence between `all`, a group and a host is ANSIBLE's to apply at resolution, not something this tool decides.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in varSetIn) (*mcp.CallToolResult, *terrapod.InventoryVar, error) {
		scopes := 0
		for _, id := range []string{in.HostID, in.GroupID, in.WorkspaceID} {
			if id != "" {
				scopes++
			}
		}
		if in.Key == "" {
			return errText("key is required"), nil, nil
		}
		if scopes != 1 {
			return errText("pass exactly one of host_id, group_id or workspace_id to say where the variable lives"), nil, nil
		}

		structured := in.Structured != nil && *in.Structured
		sensitive := in.Sensitive != nil && *in.Sensitive
		create := terrapod.CreateInventoryVarRequest{
			Key: in.Key, Value: in.Value, Structured: structured, Sensitive: sensitive,
		}
		update := terrapod.UpdateInventoryVarRequest{
			Value: &in.Value, Structured: &structured, Sensitive: &sensitive,
		}

		// Create, and on a conflict find the existing row and patch it: "set"
		// has to mean set, and an agent asked to change a variable should not
		// have to know whether it is already there. The listing is scoped to
		// the one parent, so this is one extra call on the update path only.
		switch {
		case in.HostID != "":
			v, err := c.CreateInventoryHostVar(ctx, in.HostID, create)
			if err == nil {
				return nil, v, nil
			}
			if !isConflict(err) {
				return errResult(err), nil, nil
			}
			existing, lerr := c.ListInventoryHostVars(ctx, in.HostID)
			if lerr != nil {
				return errResult(lerr), nil, nil
			}
			id, ok := findVarID(existing, in.Key)
			if !ok {
				return errResult(err), nil, nil
			}
			v, err = c.UpdateInventoryHostVar(ctx, id, update)
			if err != nil {
				return errResult(err), nil, nil
			}
			return nil, v, nil
		case in.GroupID != "":
			v, err := c.CreateInventoryGroupVar(ctx, in.GroupID, create)
			if err == nil {
				return nil, v, nil
			}
			if !isConflict(err) {
				return errResult(err), nil, nil
			}
			existing, lerr := c.ListInventoryGroupVars(ctx, in.GroupID)
			if lerr != nil {
				return errResult(lerr), nil, nil
			}
			id, ok := findVarID(existing, in.Key)
			if !ok {
				return errResult(err), nil, nil
			}
			v, err = c.UpdateInventoryGroupVar(ctx, id, update)
			if err != nil {
				return errResult(err), nil, nil
			}
			return nil, v, nil
		default:
			v, err := c.CreateInventoryGlobalVar(ctx, in.WorkspaceID, create)
			if err == nil {
				return nil, v, nil
			}
			if !isConflict(err) {
				return errResult(err), nil, nil
			}
			existing, lerr := c.ListInventoryGlobalVars(ctx, in.WorkspaceID)
			if lerr != nil {
				return errResult(lerr), nil, nil
			}
			id, ok := findVarID(existing, in.Key)
			if !ok {
				return errResult(err), nil, nil
			}
			v, err = c.UpdateInventoryGlobalVar(ctx, id, update)
			if err != nil {
				return errResult(err), nil, nil
			}
			return nil, v, nil
		}
	})

	// ── terrapod_inventory_remove ────────────────────────────────────
	type removeIn struct {
		ID string `json:"id" jsonschema:"the id of the row to remove. The typed prefix says what it is: invhost- a host, invgroup- a group, invhg- a membership, invgc- a nesting, invhvar-/invgvar-/invvar- a variable, ws- a workspace's inventory settings"`
	}
	type removeOut struct {
		Removed string `json:"removed"`
		ID      string `json:"id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_inventory_remove",
		Description: "Remove one ansible inventory row. The typed id prefix says what it is, so there is no kind to pass and no way to delete a different kind than intended. " +
			"CASCADES, and this is the part to show a user before confirming: removing a HOST removes its variables and its group memberships; removing a GROUP removes its variables, its memberships and its nestings in both directions, though the hosts themselves remain. Removing a membership or a nesting removes only that link. " +
			"Removing a workspace's inventory settings (a ws- id) clears its git binding and leaves the declared rows as the whole inventory. " +
			"Call terrapod_inventory_resolved first if the blast radius matters: a host removed here leaves every configure that targeted it.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in removeIn) (*mcp.CallToolResult, removeOut, error) {
		kinds := []struct {
			prefix, label string
			del           func(context.Context, string) error
		}{
			// `invgvar-` before `invg…` would be ambiguous the other way
			// round, so the longer prefixes are listed first and matched in
			// order rather than by a map.
			{"invhost-", "host", c.DeleteInventoryHost},
			{"invgroup-", "group", c.DeleteInventoryGroup},
			{"invhvar-", "host variable", c.DeleteInventoryHostVar},
			{"invgvar-", "group variable", c.DeleteInventoryGroupVar},
			{"invvar-", "variable under all", c.DeleteInventoryGlobalVar},
			{"invhg-", "group membership", c.DeleteInventoryHostGroup},
			{"invgc-", "group nesting", c.DeleteInventoryGroupChild},
			{"ws-", "inventory settings", c.DeleteInventorySettings},
		}
		if in.ID == "" {
			return errText("id is required"), removeOut{}, nil
		}
		for _, k := range kinds {
			if !strings.HasPrefix(in.ID, k.prefix) {
				continue
			}
			if err := k.del(ctx, in.ID); err != nil {
				return errResult(err), removeOut{}, nil
			}
			return nil, removeOut{Removed: k.label, ID: in.ID}, nil
		}
		return errText("id is not an inventory row: expected one of invhost-, invgroup-, " +
			"invhvar-, invgvar-, invvar-, invhg-, invgc- or ws-, got " + in.ID), removeOut{}, nil
	})
}

// ── Internal helpers ─────────────────────────────────────────────────────────

// isConflict says the write failed because the row already exists, which is
// the one error "set" recovers from by patching instead.
func isConflict(err error) bool {
	var c *terrapod.ConflictError
	return errors.As(err, &c)
}

// findVarID locates an existing variable by key within one parent's variables.
func findVarID(vars []terrapod.InventoryVar, key string) (string, bool) {
	for i := range vars {
		if vars[i].Key == key {
			return vars[i].ID, true
		}
	}
	return "", false
}

// orEmptyVars and its siblings replace a nil slice with an empty one: `null`
// fails the tool's derived output schema and costs the agent the whole result,
// where an empty list is the common and correct answer.
func orEmptyVars(v []terrapod.InventoryVar) []terrapod.InventoryVar {
	if v == nil {
		return []terrapod.InventoryVar{}
	}
	return v
}

func orEmptyMemberships(v []terrapod.InventoryHostGroup) []terrapod.InventoryHostGroup {
	if v == nil {
		return []terrapod.InventoryHostGroup{}
	}
	return v
}

func orEmptyNestings(v []terrapod.InventoryGroupChild) []terrapod.InventoryGroupChild {
	if v == nil {
		return []terrapod.InventoryGroupChild{}
	}
	return v
}
