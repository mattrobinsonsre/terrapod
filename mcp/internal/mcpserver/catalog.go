package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerCatalog adds the service-catalog tools (#535, #2019): the provider
// templates and catalog items an administrator publishes, and the provision +
// lifecycle flow over the instances a user creates from them.
//
// # Why the whole surface is here rather than a curated slice
//
// The MCP server is NOT an authorization boundary. It carries the caller's own
// `tofu login` token and the API applies the same catalog RBAC it applies to
// the UI, the Terraform provider and `curl`, so withholding an item attribute
// or a lifecycle action protects nobody — whatever the caller's role permits
// they reach through another door, and whatever it forbids is refused at every
// door alike. What an omission DOES produce is an asymmetry: an agent that can
// read an item and not publish one, or provision an instance and not confirm
// its run, which invites it to describe an action it cannot take. So
// completeness is the default, and risk is signalled by ANNOTATION —
// `readOnly`, `mutating`, `destructive` — never by absence.
//
// # Why provision, reconfigure, destroy and confirm are all `destructive`
//
// `mutating` states in its own doc comment that the tool "does not itself
// change or destroy infrastructure". A provision queues a run that is never
// plan-only and creates real cloud resources; a reconfigure can force a
// replacement; a confirm IS the apply. All of them therefore carry
// `destructive`, for the same reason `terrapod_run_create` does even though it
// defaults to plan-only. Only `terrapod_catalog_instance_discard` is
// `mutating`, matching `terrapod_run_discard`.
//
// # The confirm/discard pair is the catalog's own, not a copy of a run tool
//
// A catalog instance's workspace is clamped: every non-platform-admin grant on
// it is capped at read, so the person who provisioned it CANNOT confirm its run
// through `terrapod_run_apply`. The catalog surface is the instance's control
// plane, which is why `terrapod_catalog_instance_confirm` has to exist.
//
// # The read of the module's own interface lives in ground.go
//
// `terrapod_catalog_item_interface` is registered beside the registry tools,
// because it answers a module question rather than a catalog one. It and
// `terrapod_catalog_item_form` are the pair to read before provisioning: the
// interface is the module's whole surface, the form is the curated subset a
// provisioner fills in.
func registerCatalog(s *mcp.Server, c *terrapod.Client) {
	registerCatalogItems(s, c)
	registerProviderTemplates(s, c)
	registerCatalogInstances(s, c)
}

// ── Shared output shapes ───────────────────────────────────────────────
//
// The SDK's CatalogItem, ProviderTemplate and CatalogInstance each carry their
// server attributes as an open-ended `map[string]any` with NO json tags, so
// returning one straight to an agent would hand it PascalCase keys under an
// `Attributes` wrapper. Worse, the map is neither optional nor nullable in the
// generated output schema, so on an error path — where the tool returns a nil
// pointer beside the error result — the MCP SDK's own output validation fails
// with `/properties/Attributes: has type "null", want "object"` and the
// refusal never reaches the caller. These wrappers fix both: lowercase keys,
// and `omitempty` throughout so the zero value validates.
//
// CatalogRunRef needs none of this: it is three scalars, so a nil one
// validates and its field names are already readable.

type catalogItemOut struct {
	ID         string         `json:"id,omitempty"`
	Attributes map[string]any `json:"attributes,omitempty"`
	CreatedAt  string         `json:"created_at,omitempty"`
	UpdatedAt  string         `json:"updated_at,omitempty"`
}

func itemOut(i *terrapod.CatalogItem) catalogItemOut {
	if i == nil {
		return catalogItemOut{}
	}
	return catalogItemOut{ID: i.ID, Attributes: i.Attributes, CreatedAt: i.CreatedAt, UpdatedAt: i.UpdatedAt}
}

type providerTemplateOut struct {
	ID         string         `json:"id,omitempty"`
	Attributes map[string]any `json:"attributes,omitempty"`
	CreatedAt  string         `json:"created_at,omitempty"`
	UpdatedAt  string         `json:"updated_at,omitempty"`
}

func templateOut(t *terrapod.ProviderTemplate) providerTemplateOut {
	if t == nil {
		return providerTemplateOut{}
	}
	return providerTemplateOut{ID: t.ID, Attributes: t.Attributes, CreatedAt: t.CreatedAt, UpdatedAt: t.UpdatedAt}
}

type catalogInstanceOut struct {
	ID         string         `json:"id,omitempty"`
	Attributes map[string]any `json:"attributes,omitempty"`
}

func instanceOut(i *terrapod.CatalogInstance) catalogInstanceOut {
	if i == nil {
		return catalogInstanceOut{}
	}
	return catalogInstanceOut{ID: i.ID, Attributes: i.Attributes}
}

func itemsOut(items []terrapod.CatalogItem) []catalogItemOut {
	out := make([]catalogItemOut, 0, len(items))
	for i := range items {
		out = append(out, itemOut(&items[i]))
	}
	return out
}

func templatesOut(templates []terrapod.ProviderTemplate) []providerTemplateOut {
	out := make([]providerTemplateOut, 0, len(templates))
	for i := range templates {
		out = append(out, templateOut(&templates[i]))
	}
	return out
}

func instancesOut(instances []terrapod.CatalogInstance) []catalogInstanceOut {
	out := make([]catalogInstanceOut, 0, len(instances))
	for i := range instances {
		out = append(out, instanceOut(&instances[i]))
	}
	return out
}

// ── Shared input shapes ────────────────────────────────────────────────

// variableOptionIn is one entry in a catalog item's `variable-options`: the
// item's override of how a module input is presented on the provision form.
type variableOptionIn struct {
	Name    string `json:"name" jsonschema:"the module input variable this entry overrides, exactly as the module declares it. An entry naming a variable the module does not have is accepted and simply never matches, so read the module's inputs with terrapod_catalog_item_interface first"`
	Options []any  `json:"options,omitempty" jsonschema:"restrict the input to a closed list of permitted values, rendered as a picker on the form. Omit for a free-text field"`
	Hidden  *bool  `json:"hidden,omitempty" jsonschema:"remove the input from the provision form entirely and wire this entry's default instead. A hidden entry MUST carry a default (422 otherwise): it can neither be supplied nor wired without one, and a required module input would then fail opaquely at plan time. This is how an item fixes a value a provisioner must not choose, such as an account id or a region"`
	Default any    `json:"default,omitempty" jsonschema:"the value to pre-fill on the form, or -- with hidden true -- to wire unconditionally. Overrides the module's own default for this item"`
}

// providerTemplateParameterIn is one parameter of a provider template. It
// becomes an extra field on the provision form of every catalog item that
// references the template.
type providerTemplateParameterIn struct {
	Name        string `json:"name" jsonschema:"the parameter name, as the template body interpolates it"`
	Type        string `json:"type,omitempty" jsonschema:"the value's type, for form rendering (default string)"`
	Description string `json:"description,omitempty" jsonschema:"what to tell the provisioner on the form"`
	Required    *bool  `json:"required,omitempty" jsonschema:"whether a provision must supply it; default false"`
	Sensitive   *bool  `json:"sensitive,omitempty" jsonschema:"mask the value on the form and in responses; default false"`
	Default     any    `json:"default,omitempty" jsonschema:"the value to pre-fill when a provision does not supply one"`
	Options     []any  `json:"options,omitempty" jsonschema:"restrict the parameter to a closed list of permitted values"`
}

// ── Catalog items ──────────────────────────────────────────────────────

func registerCatalogItems(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_catalog_item_list ───────────────────────────────────
	type itemListOut struct {
		Count int              `json:"count"`
		Items []catalogItemOut `json:"items"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_list",
		Description: "List the service-catalog items published on this instance — the blessed designations over private registry modules that a user provisions from WITHOUT writing any Terraform. " +
			"Each item carries its linked module, its version pin, which agent pools it permits, the form overrides it applies to the module's inputs, its labels and owner, and the ANSIBLE half it provisions (`configure-enabled`, `ansible-directory`, `default-playbook`, `default-limit`). " +
			"Three attributes read wrongly if taken at face value. `default-version-pin` empty or null means the item FLOATS to the module's latest uploaded version — it is not an unset, broken item. `allowed-agent-pool-ids` null means every pool the caller can assign is permitted, where an empty LIST permits none: two different answers that both look like nothing. `enabled: false` is a published item that refuses to provision (409), which is how one is retired without deleting it and orphaning its instances. " +
			"Returns only the items the caller has at least catalog read on, so a short list is as much a permission answer as an inventory of what exists. " +
			"The whole catalog surface answers 404 when the deployment has the catalog feature off, so a 404 here means \"the feature is not enabled\", NOT \"there are no items\".",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, itemListOut, error) {
		items, err := c.ListCatalogItems(ctx)
		if err != nil {
			return errResult(err), itemListOut{}, nil
		}
		return nil, itemListOut{Count: len(items), Items: itemsOut(items)}, nil
	})

	// ── terrapod_catalog_item_get ────────────────────────────────────
	type itemIDIn struct {
		CatalogItemID string `json:"catalog_item_id" jsonschema:"the catalog item's id (a bare UUID)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_get",
		Description: "Get one service-catalog item by id — its linked module, version pin, permitted agent pools, form overrides, labels, owner, and the Ansible half it provisions. " +
			"The same three reading traps as the list: `default-version-pin` empty or null means it FLOATS to the module's latest uploaded version; `allowed-agent-pool-ids` null means any pool the caller can assign while an empty list permits none; `enabled: false` is a published item that refuses to provision. " +
			"`configure-enabled` and `ansible-directory` answer DIFFERENT questions — whether the provisioned workspace has an Ansible configure operation, and which subdirectory of the module holds it — so an empty `ansible-directory` does not mean the operation is off, and a non-empty one does not mean it is on. " +
			"For what an instance of it would actually take, read terrapod_catalog_item_form (the fields a provisioner fills in) rather than inferring it from here; terrapod_catalog_item_interface is the module's own whole surface, including its outputs. " +
			"Needs catalog read on the item; a 404 also means the catalog feature is off on this deployment.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in itemIDIn) (*mcp.CallToolResult, catalogItemOut, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), catalogItemOut{}, nil
		}
		item, err := c.GetCatalogItem(ctx, in.CatalogItemID)
		if err != nil {
			return errResult(err), catalogItemOut{}, nil
		}
		return nil, itemOut(item), nil
	})

	// ── terrapod_catalog_item_form ───────────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_form",
		Description: "Get the provision FORM for a service-catalog item: the fields a provisioner actually fills in, which is the curated subset of the module's inputs PLUS every parameter of every provider template the item references. " +
			"This is what terrapod_catalog_provision's `input_values` must be built from. The provision call refuses an input that is not on this form (422 — it is the door a non-exposed module variable would otherwise be injected through), and refuses a required field with no default left unsupplied, so guessing from the module's variables instead fails on both counts. " +
			"Each field carries `name`, `type`, `description`, `required`, `sensitive`, `default`, `options` (a closed list when the item restricts the value) and `source` — `module` for a module input, `provider:<template name>` for a provider-template parameter. " +
			"HIDDEN inputs are deliberately ABSENT: an item may fix a module input to a constant, which is wired without being presented, so a field missing here is not necessarily one the module does not have — compare against terrapod_catalog_item_interface for the module's whole surface. " +
			"`resolved-version` is the module version this item currently resolves to. Read `interface-error`: when it is set the module's files could not be parsed, so an empty `fields` list does not mean the item takes no inputs. " +
			"Needs catalog read on the item.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in itemIDIn) (*mcp.CallToolResult, map[string]any, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), nil, nil
		}
		form, err := c.GetCatalogItemForm(ctx, in.CatalogItemID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, form, nil
	})

	// ── terrapod_catalog_item_create ─────────────────────────────────
	//
	// Each field is sent only when the caller supplied it, so the server's own
	// defaults stand for the rest — and the Ansible half's four attributes
	// default to off and empty, which is what makes a Terraform-only item the
	// ordinary case rather than one that has to say so.
	type itemCreateIn struct {
		Name                string             `json:"name" jsonschema:"the item's name, unique across the catalog. A 409 means the name is taken"`
		ModuleID            string             `json:"module_id" jsonschema:"the private registry module this item designates, as a bare UUID (NOT a name/provider pair). Read it from terrapod_registry_module_get, whose id field is this value"`
		DisplayName         string             `json:"display_name,omitempty" jsonschema:"the label shown in the catalog UI; omit to show the name"`
		Description         string             `json:"description,omitempty" jsonschema:"what this item provisions, for the person choosing it"`
		Enabled             *bool              `json:"enabled,omitempty" jsonschema:"whether the item may be provisioned from. DEFAULTS TO TRUE, so an ordinary item says nothing here. An item with this false is still published and readable but refuses to provision (409), which is how one is retired without deleting it and orphaning its instances"`
		DefaultVersionPin   string             `json:"default_version_pin,omitempty" jsonschema:"pin every provision to this module version. OMIT OR PASS EMPTY TO FLOAT, which resolves to the module's latest uploaded version at each provision and reconfigure -- so leaving it empty is a deliberate tracking choice rather than an unset field"`
		ProviderTemplateIDs []string           `json:"provider_template_ids,omitempty" jsonschema:"the provider templates rendered into every instance's generated providers.tf, as bare UUIDs (terrapod_provider_template_list). Each template's parameters become extra fields on this item's provision form. A UUID that is not a known template is refused (422)"`
		AllowedAgentPoolIDs []string           `json:"allowed_agent_pool_ids,omitempty" jsonschema:"restrict provisioning to these agent pools (apool-... or a bare UUID). OMIT to permit any pool the provisioner can assign, which is the default. An EMPTY LIST is not the same as omitting it: it permits no pool at all, so every provision is then refused 403"`
		VariableOptions     []variableOptionIn `json:"variable_options,omitempty" jsonschema:"per-input overrides of how the module's variables appear on the provision form: a closed list of permitted values, a different default, or hidden-with-a-fixed-value"`
		Labels              map[string]string  `json:"labels,omitempty" jsonschema:"key/value labels for catalog RBAC and filtering. Reserved keys are refused (422)"`

		// The Ansible half this item provisions (#2019). Two independent
		// questions, deliberately not folded into one field: #1966 corrected
		// exactly that on the workspace's own config-directory, where an empty
		// path was being read as "no configure operation".
		ConfigureEnabled *bool  `json:"configure_enabled,omitempty" jsonschema:"whether the workspace this item provisions has an ANSIBLE CONFIGURE operation. DEFAULTS TO FALSE, so a Terraform-only item says nothing here. This says WHETHER; ansible_directory says WHERE, and the path does NOT carry off in its emptiness. Setting this true with no ansible_directory is REFUSED (422): it is incoherent rather than a no-op, because it would provision a workspace whose Ansible root is the configuration version's root, where only the generated wrapper lives, and fail at run time for want of a playbook"`
		AnsibleDirectory string `json:"ansible_directory,omitempty" jsonschema:"the subdirectory OF THE MODULE that holds the Ansible configuration. At provision its whole subtree is COPIED into the generated configuration version at the same relative path -- not referenced from it, because a composite run's configure Job re-fetches the configuration version and never runs init, so there is no .terraform/modules/ for it to point at. The provisioned workspace's config_directory is then this same path. Relative to the module root and stored BYTE FOR BYTE: a leading or trailing slash, a '..' segment, leading or trailing whitespace, or more than 255 characters are each REFUSED (422) rather than cleaned up, because the value round-trips through the Terraform provider and silently storing a different one than you sent fails the apply with 'Provider produced inconsistent result after apply'. Three further refusals land at PROVISION rather than here: the module has no uploaded version to copy from; this names the module ROOT (empty, '.', or a trailing slash) rather than a subdirectory; or the subtree holds no regular file, in which case the error names what the module does ship. Only regular files are copied -- a symlink, hard link or device entry is dropped, because this repacks into a tarball a runner extracts"`
		DefaultPlaybook  string `json:"default_playbook,omitempty" jsonschema:"the playbook templated onto the provisioned workspace as its default_playbook, relative to ansible_directory. Empty means the workspace gets no default and every configure run on it must name its own playbook. Same byte-for-byte validation as ansible_directory: no leading or trailing slash, no '..', no stray whitespace, 255 characters at most, each refused rather than normalised"`
		DefaultLimit     string `json:"default_limit,omitempty" jsonschema:"the ansible --limit templated onto the provisioned workspace as its default_limit, overridable per run. EMPTY MEANS THE WHOLE INVENTORY, which is ansible's own default -- so this is the blast radius of every triggered configure on the instances this item creates, and naming a canary group here is what bounds them. Stored byte for byte; leading or trailing whitespace is refused (422) rather than stripped"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_create",
		Description: "Publish a service-catalog item: a blessed designation over a private registry module that a user provisions from without writing Terraform. `name` and `module_id` are required; everything else falls back to the server's default. " +
			"An item's job is to NARROW the module — pin a version, restrict which agent pools may run it, fix or constrain its inputs, and attach the provider configuration so a provisioner never writes one or holds the credentials it names. " +
			"It may also declare an ANSIBLE half, as two independent fields: `configure_enabled` says whether the provisioned workspace has a configure operation (default false), and `ansible_directory` says which subdirectory of the module holds it. Both or neither — `configure_enabled` true with no directory is refused 422, and setting the directory alone turns nothing on. " +
			"Read the module's own inputs first with terrapod_registry_module_interface so `variable_options` names variables that exist; an entry naming one that does not is accepted and simply never matches. " +
			"Platform admin only. Returns the created item.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in itemCreateIn) (*mcp.CallToolResult, catalogItemOut, error) {
		if in.Name == "" || in.ModuleID == "" {
			return errText("name and module_id are both required"), catalogItemOut{}, nil
		}
		attrs := map[string]any{"name": in.Name, "module-id": in.ModuleID}
		if in.DisplayName != "" {
			attrs["display-name"] = in.DisplayName
		}
		if in.Description != "" {
			attrs["description"] = in.Description
		}
		if in.Enabled != nil {
			attrs["enabled"] = *in.Enabled
		}
		if in.DefaultVersionPin != "" {
			attrs["default-version-pin"] = in.DefaultVersionPin
		}
		if in.ProviderTemplateIDs != nil {
			attrs["provider-template-ids"] = in.ProviderTemplateIDs
		}
		if in.AllowedAgentPoolIDs != nil {
			attrs["allowed-agent-pool-ids"] = in.AllowedAgentPoolIDs
		}
		if in.VariableOptions != nil {
			attrs["variable-options"] = variableOptionAttrs(in.VariableOptions)
		}
		if in.Labels != nil {
			attrs["labels"] = in.Labels
		}
		if in.ConfigureEnabled != nil {
			attrs["configure-enabled"] = *in.ConfigureEnabled
		}
		if in.AnsibleDirectory != "" {
			attrs["ansible-directory"] = in.AnsibleDirectory
		}
		if in.DefaultPlaybook != "" {
			attrs["default-playbook"] = in.DefaultPlaybook
		}
		if in.DefaultLimit != "" {
			attrs["default-limit"] = in.DefaultLimit
		}
		item, err := c.CreateCatalogItem(ctx, attrs)
		if err != nil {
			return errResult(err), catalogItemOut{}, nil
		}
		return nil, itemOut(item), nil
	})

	// ── terrapod_catalog_item_update ─────────────────────────────────
	//
	// Every string is a pointer, because on a PATCH the empty string is a
	// MEANING rather than an absence: clearing `ansible_directory` is how the
	// Ansible half is withdrawn, and emptying `default_version_pin` is how an
	// item moves from a pin to floating. A plain string would make both
	// indistinguishable from "leave it alone", which is the state a caller
	// changing one other field is in.
	type itemUpdateIn struct {
		CatalogItemID       string             `json:"catalog_item_id" jsonschema:"the catalog item's id (a bare UUID)"`
		Name                *string            `json:"name,omitempty" jsonschema:"rename the item. A 409 means the new name is taken"`
		ModuleID            *string            `json:"module_id,omitempty" jsonschema:"relink the item to a different registry module, as a bare UUID. Existing instances keep the module they were provisioned from until their next reconfigure, which then moves them"`
		DisplayName         *string            `json:"display_name,omitempty" jsonschema:"the label shown in the catalog UI; pass empty to clear it and fall back to the name"`
		Description         *string            `json:"description,omitempty" jsonschema:"what this item provisions; pass empty to clear it"`
		Enabled             *bool              `json:"enabled,omitempty" jsonschema:"whether the item may be provisioned from. Setting it false retires the item without deleting it: it stays readable, its instances are untouched and still reconfigurable, and a new provision is refused 409"`
		DefaultVersionPin   *string            `json:"default_version_pin,omitempty" jsonschema:"pin every future provision and reconfigure to this module version. PASS EMPTY TO FLOAT instead, which resolves to the module's latest uploaded version each time -- so empty here is a deliberate change of tracking mode rather than a cleared field"`
		ProviderTemplateIDs []string           `json:"provider_template_ids,omitempty" jsonschema:"REPLACE the provider templates rendered into future instances, as bare UUIDs. An empty list detaches all of them, which also removes their parameters from the provision form"`
		AllowedAgentPoolIDs []string           `json:"allowed_agent_pool_ids,omitempty" jsonschema:"REPLACE the agent pools this item may be provisioned onto (apool-... or a bare UUID). An EMPTY LIST permits none, so every provision is then refused 403 -- to LIFT the restriction entirely pass allow_any_agent_pool instead"`
		AllowAnyAgentPool   bool               `json:"allow_any_agent_pool,omitempty" jsonschema:"lift the pool restriction altogether, permitting any pool the provisioner can assign. This is the only way to express it, because an empty allowed_agent_pool_ids list means the opposite (no pool is permitted). Refused together with allowed_agent_pool_ids"`
		VariableOptions     []variableOptionIn `json:"variable_options,omitempty" jsonschema:"REPLACE the per-input form overrides. An empty list removes all of them, which returns every module input to the form with the module's own default"`
		Labels              map[string]string  `json:"labels,omitempty" jsonschema:"REPLACE the item's labels. An empty object clears them, which also removes whatever catalog RBAC was keyed on them. Reserved keys are refused (422)"`

		ConfigureEnabled *bool   `json:"configure_enabled,omitempty" jsonschema:"whether the workspace this item provisions has an ANSIBLE CONFIGURE operation. This says WHETHER and ansible_directory says WHERE, so clearing the path does not turn the operation off and setting it does not turn it on. Turning it ON needs a directory -- either already stored or sent in this same call -- or the update is refused 422. Turning it OFF leaves existing instances alone until their next reconfigure, which then removes the operation from the workspace rather than leaving a stale directory and a playbook it no longer has"`
		AnsibleDirectory *string `json:"ansible_directory,omitempty" jsonschema:"the subdirectory OF THE MODULE that holds the Ansible configuration. Its whole subtree is COPIED into each instance's generated configuration version at the same relative path -- not referenced from it, because a composite run's configure Job re-fetches the configuration version and never runs init, so there is no .terraform/modules/ for it to point at -- and that same path becomes the provisioned workspace's config_directory. PASS EMPTY TO CLEAR IT, which is refused (422) while configure_enabled is true: send configure_enabled false in the same call to withdraw the Ansible half. Stored BYTE FOR BYTE: a leading or trailing slash, a '..' segment, leading or trailing whitespace, or more than 255 characters are each refused (422) rather than cleaned up, because the value round-trips through the Terraform provider and silently storing a different one than you sent fails the apply with 'Provider produced inconsistent result after apply'"`
		DefaultPlaybook  *string `json:"default_playbook,omitempty" jsonschema:"the playbook templated onto future instances as their default_playbook, relative to ansible_directory. Pass empty to clear it, after which every configure run on an instance must name its own playbook. Stored byte for byte like ansible_directory: a leading or trailing slash, a '..' segment, stray whitespace, or more than 255 characters are each refused (422) rather than normalised"`
		DefaultLimit     *string `json:"default_limit,omitempty" jsonschema:"the ansible --limit templated onto future instances as their default_limit. PASS EMPTY TO MEAN THE WHOLE INVENTORY, which is ansible's own default and therefore a WIDENING of the blast radius of every triggered configure on them rather than a cleared field. Leading or trailing whitespace is refused (422) rather than stripped"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_update",
		Description: "Update a service-catalog item. Only the fields you pass change, and every list or map field you do pass REPLACES the stored one rather than merging into it — so read the item first (terrapod_catalog_item_get) and send the whole set. " +
			"An empty value is a meaning rather than an absence on four of these: empty `default_version_pin` moves the item from a pin to FLOATING; empty `default_limit` widens every triggered configure on its instances to the WHOLE inventory; an empty `allowed_agent_pool_ids` list permits NO pool (use `allow_any_agent_pool` to lift the restriction); and empty `labels` clears whatever catalog RBAC was keyed on them. " +
			"Changes apply to future provisions and to each existing instance at its NEXT RECONFIGURE — nothing is pushed to a running instance by updating the item. " +
			"The Ansible half stays two independent questions: `configure_enabled` cannot be true without an `ansible_directory` (422), and clearing the directory while it is true is refused for the same reason, so withdrawing the operation means sending `configure_enabled: false` in the same call. " +
			"Platform admin only. Returns the updated item.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in itemUpdateIn) (*mcp.CallToolResult, catalogItemOut, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), catalogItemOut{}, nil
		}
		if in.AllowAnyAgentPool && in.AllowedAgentPoolIDs != nil {
			return errText("pass allowed_agent_pool_ids to restrict the pools, or allow_any_agent_pool " +
				"to lift the restriction — not both: they are opposite answers to one question"), catalogItemOut{}, nil
		}
		attrs := map[string]any{}
		if in.Name != nil {
			attrs["name"] = *in.Name
		}
		if in.ModuleID != nil {
			attrs["module-id"] = *in.ModuleID
		}
		if in.DisplayName != nil {
			attrs["display-name"] = *in.DisplayName
		}
		if in.Description != nil {
			attrs["description"] = *in.Description
		}
		if in.Enabled != nil {
			attrs["enabled"] = *in.Enabled
		}
		if in.DefaultVersionPin != nil {
			attrs["default-version-pin"] = *in.DefaultVersionPin
		}
		if in.ProviderTemplateIDs != nil {
			attrs["provider-template-ids"] = in.ProviderTemplateIDs
		}
		switch {
		case in.AllowAnyAgentPool:
			attrs["allowed-agent-pool-ids"] = nil
		case in.AllowedAgentPoolIDs != nil:
			attrs["allowed-agent-pool-ids"] = in.AllowedAgentPoolIDs
		}
		if in.VariableOptions != nil {
			attrs["variable-options"] = variableOptionAttrs(in.VariableOptions)
		}
		if in.Labels != nil {
			attrs["labels"] = in.Labels
		}
		if in.ConfigureEnabled != nil {
			attrs["configure-enabled"] = *in.ConfigureEnabled
		}
		if in.AnsibleDirectory != nil {
			attrs["ansible-directory"] = *in.AnsibleDirectory
		}
		if in.DefaultPlaybook != nil {
			attrs["default-playbook"] = *in.DefaultPlaybook
		}
		if in.DefaultLimit != nil {
			attrs["default-limit"] = *in.DefaultLimit
		}
		if len(attrs) == 0 {
			return errText("pass at least one field to change"), catalogItemOut{}, nil
		}
		item, err := c.UpdateCatalogItem(ctx, in.CatalogItemID, attrs)
		if err != nil {
			return errResult(err), catalogItemOut{}, nil
		}
		return nil, itemOut(item), nil
	})

	// ── terrapod_catalog_item_delete ─────────────────────────────────
	type itemDeleteOut struct {
		Deleted       bool   `json:"deleted"`
		CatalogItemID string `json:"catalog_item_id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_item_delete",
		Description: "Delete a service-catalog item. This removes the DESIGNATION only — it touches no infrastructure and no registry module, and the module stays published and provisionable through any other item over it. " +
			"REFUSED with 409 while the item has any ACTIVE instance, naming how many: destroy those first (terrapod_catalog_instance_destroy), because the item is what records where an instance's configuration came from and what its next reconfigure would render. Already-archived instances do not hold it. " +
			"Prefer terrapod_catalog_item_update with `enabled: false` when the intent is only to stop new provisions — that retires the item reversibly and leaves its instances reconfigurable, where this does not. " +
			"Confirm with the user before calling it. Platform admin only.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in itemIDIn) (*mcp.CallToolResult, itemDeleteOut, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), itemDeleteOut{}, nil
		}
		if err := c.DeleteCatalogItem(ctx, in.CatalogItemID); err != nil {
			return errResult(err), itemDeleteOut{}, nil
		}
		return nil, itemDeleteOut{Deleted: true, CatalogItemID: in.CatalogItemID}, nil
	})
}

// variableOptionAttrs renders the typed form overrides as the wire shape,
// omitting what the caller left alone so the server's own handling stands. It
// matters most on `default`: sending an explicit null on a hidden entry would
// earn a 422 the call never asked for.
func variableOptionAttrs(opts []variableOptionIn) []map[string]any {
	out := make([]map[string]any, 0, len(opts))
	for _, o := range opts {
		entry := map[string]any{"name": o.Name}
		if o.Options != nil {
			entry["options"] = o.Options
		}
		if o.Hidden != nil {
			entry["hidden"] = *o.Hidden
		}
		if o.Default != nil {
			entry["default"] = o.Default
		}
		out = append(out, entry)
	}
	return out
}

// ── Provider templates ─────────────────────────────────────────────────

func registerProviderTemplates(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_provider_template_list ──────────────────────────────
	type templateListOut struct {
		Count     int                   `json:"count"`
		Templates []providerTemplateOut `json:"templates"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_provider_template_list",
		Description: "List the provider templates on this instance: the parameterised `provider` blocks an administrator writes once and attaches to catalog items, rendered into each instance's generated `providers.tf`. " +
			"They are what lets a provisioner create working infrastructure without writing provider configuration or holding the credentials it names — an item references a template by id, and the template's `parameters` become extra fields on that item's provision form. " +
			"Read this to get the UUIDs for `provider_template_ids` on terrapod_catalog_item_create; a UUID that is not a known template is refused 422. " +
			"Admin or audit; a 404 also means the catalog feature is off on this deployment.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, templateListOut, error) {
		templates, err := c.ListProviderTemplates(ctx)
		if err != nil {
			return errResult(err), templateListOut{}, nil
		}
		return nil, templateListOut{Count: len(templates), Templates: templatesOut(templates)}, nil
	})

	// ── terrapod_provider_template_get ───────────────────────────────
	type templateIDIn struct {
		ProviderTemplateID string `json:"provider_template_id" jsonschema:"the provider template's id (a bare UUID)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_provider_template_get",
		Description: "Get one provider template by id — its provider type, its HCL body, and the parameters that body interpolates. " +
			"The parameters are the load-bearing part: each one becomes a field on the provision form of every catalog item referencing this template, so changing them changes what a provision must supply. " +
			"Admin or audit.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in templateIDIn) (*mcp.CallToolResult, providerTemplateOut, error) {
		if in.ProviderTemplateID == "" {
			return errText("provider_template_id is required"), providerTemplateOut{}, nil
		}
		t, err := c.GetProviderTemplate(ctx, in.ProviderTemplateID)
		if err != nil {
			return errResult(err), providerTemplateOut{}, nil
		}
		return nil, templateOut(t), nil
	})

	// ── terrapod_provider_template_create ────────────────────────────
	type templateCreateIn struct {
		Name         string                        `json:"name" jsonschema:"the template's name, unique across the deployment. It is user-visible: a form field derived from this template reports its source as provider:<name>. A 409 means the name is taken"`
		ProviderType string                        `json:"provider_type" jsonschema:"the provider this configures, as a provider block names it (aws, google, vault)"`
		Body         string                        `json:"body" jsonschema:"the HCL body of the provider block, interpolating this template's parameters. Rendered verbatim into each instance's generated providers.tf, so it is the one place provider credentials and endpoints are decided for everything provisioned from the items referencing it"`
		Parameters   []providerTemplateParameterIn `json:"parameters,omitempty" jsonschema:"the values the body interpolates, each becoming a field on the provision form of every item that references this template. Omit for a template that needs nothing from the provisioner"`
		Labels       map[string]string             `json:"labels,omitempty" jsonschema:"key/value labels for filtering. Reserved keys are refused (422)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_provider_template_create",
		Description: "Create a provider template: a parameterised `provider` block rendered into the generated configuration of every catalog instance whose item references it. " +
			"This is how a catalog provisions working infrastructure without a provisioner writing provider configuration or holding its credentials, so the body is a privileged place — whatever it names is used by every instance of every item that references it. " +
			"`name`, `provider_type` and `body` are required. Each entry in `parameters` becomes a field on those items' provision forms, so adding one makes a value the provisioner must supply, or that carries a default. " +
			"It takes effect for an existing instance at its next reconfigure, not immediately. Platform admin only. Returns the created template.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in templateCreateIn) (*mcp.CallToolResult, providerTemplateOut, error) {
		if in.Name == "" || in.ProviderType == "" || in.Body == "" {
			return errText("name, provider_type and body are all required"), providerTemplateOut{}, nil
		}
		attrs := map[string]any{
			"name":          in.Name,
			"provider-type": in.ProviderType,
			"body":          in.Body,
		}
		if in.Parameters != nil {
			attrs["parameters"] = templateParameterAttrs(in.Parameters)
		}
		if in.Labels != nil {
			attrs["labels"] = in.Labels
		}
		t, err := c.CreateProviderTemplate(ctx, attrs)
		if err != nil {
			return errResult(err), providerTemplateOut{}, nil
		}
		return nil, templateOut(t), nil
	})

	// ── terrapod_provider_template_update ────────────────────────────
	type templateUpdateIn struct {
		ProviderTemplateID string                        `json:"provider_template_id" jsonschema:"the provider template's id (a bare UUID)"`
		Name               *string                       `json:"name,omitempty" jsonschema:"rename the template. It is user-visible as a form field's source (provider:<name>), so a rename is not purely internal. Cannot be empty. A 409 means the new name is taken"`
		ProviderType       *string                       `json:"provider_type,omitempty" jsonschema:"the provider this configures. Cannot be empty"`
		Body               *string                       `json:"body,omitempty" jsonschema:"REPLACE the HCL body. Cannot be empty. Every instance of every item referencing this template renders the new body at its next reconfigure, so a body that no longer interpolates a parameter, or interpolates one that no longer exists, breaks those instances rather than this record"`
		Parameters         []providerTemplateParameterIn `json:"parameters,omitempty" jsonschema:"REPLACE the parameters rather than merging into them. An empty list removes all of them, which also removes their fields from every referencing item's provision form -- and a stored input value for a removed field then becomes an unknown input, refused 422 at the next reconfigure"`
		Labels             map[string]string             `json:"labels,omitempty" jsonschema:"REPLACE the labels. An empty object clears them. Reserved keys are refused (422)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_provider_template_update",
		Description: "Update a provider template. Only the fields you pass change, and `parameters` and `labels` REPLACE the stored ones rather than merging — read the template first (terrapod_provider_template_get) and send the whole set. " +
			"The blast radius is wider than the record: every instance of every catalog item referencing this template renders the new body and parameter set at its NEXT RECONFIGURE, so a parameter removed here turns a stored input value into an unknown input (refused 422) on those instances, and a body change moves the provider configuration under infrastructure that is already running. " +
			"`name`, `provider_type` and `body` cannot be set to empty. Platform admin only. Returns the updated template.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in templateUpdateIn) (*mcp.CallToolResult, providerTemplateOut, error) {
		if in.ProviderTemplateID == "" {
			return errText("provider_template_id is required"), providerTemplateOut{}, nil
		}
		attrs := map[string]any{}
		if in.Name != nil {
			attrs["name"] = *in.Name
		}
		if in.ProviderType != nil {
			attrs["provider-type"] = *in.ProviderType
		}
		if in.Body != nil {
			attrs["body"] = *in.Body
		}
		if in.Parameters != nil {
			attrs["parameters"] = templateParameterAttrs(in.Parameters)
		}
		if in.Labels != nil {
			attrs["labels"] = in.Labels
		}
		if len(attrs) == 0 {
			return errText("pass at least one field to change"), providerTemplateOut{}, nil
		}
		t, err := c.UpdateProviderTemplate(ctx, in.ProviderTemplateID, attrs)
		if err != nil {
			return errResult(err), providerTemplateOut{}, nil
		}
		return nil, templateOut(t), nil
	})

	// ── terrapod_provider_template_delete ────────────────────────────
	type templateDeleteOut struct {
		Deleted            bool   `json:"deleted"`
		ProviderTemplateID string `json:"provider_template_id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_provider_template_delete",
		Description: "Delete a provider template. REFUSED with 409 while any catalog item still references it — detach it from those items first (terrapod_catalog_item_update with the remaining `provider_template_ids`), because an item rendering a template that no longer exists would generate a configuration with no provider block. " +
			"This removes the record only: it changes no infrastructure, and nothing is pushed to a running instance. Confirm with the user before calling it. Platform admin only.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in templateIDIn) (*mcp.CallToolResult, templateDeleteOut, error) {
		if in.ProviderTemplateID == "" {
			return errText("provider_template_id is required"), templateDeleteOut{}, nil
		}
		if err := c.DeleteProviderTemplate(ctx, in.ProviderTemplateID); err != nil {
			return errResult(err), templateDeleteOut{}, nil
		}
		return nil, templateDeleteOut{Deleted: true, ProviderTemplateID: in.ProviderTemplateID}, nil
	})
}

// templateParameterAttrs renders typed parameters as the wire shape, omitting
// what the caller left alone so the server's own defaults stand.
func templateParameterAttrs(params []providerTemplateParameterIn) []map[string]any {
	out := make([]map[string]any, 0, len(params))
	for _, p := range params {
		entry := map[string]any{"name": p.Name}
		if p.Type != "" {
			entry["type"] = p.Type
		}
		if p.Description != "" {
			entry["description"] = p.Description
		}
		if p.Required != nil {
			entry["required"] = *p.Required
		}
		if p.Sensitive != nil {
			entry["sensitive"] = *p.Sensitive
		}
		if p.Default != nil {
			entry["default"] = p.Default
		}
		if p.Options != nil {
			entry["options"] = p.Options
		}
		out = append(out, entry)
	}
	return out
}

// ── Catalog instances: provision and lifecycle ─────────────────────────

func registerCatalogInstances(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_catalog_provision ───────────────────────────────────
	type provisionIn struct {
		CatalogItemID string            `json:"catalog_item_id" jsonschema:"the catalog item to provision from (a bare UUID)"`
		Name          string            `json:"name" jsonschema:"the new workspace's name, unique across the deployment. A 409 means it is taken -- including when a concurrent provision won the race"`
		AgentPoolID   string            `json:"agent_pool_id" jsonschema:"the agent pool the instance runs on (apool-... or a bare UUID). REQUIRED: a catalog instance is always agent-executed. It must be one the item permits when the item restricts them (403 otherwise), and the caller needs write on it in its own right (403) -- so this is an intersection of two permissions rather than one"`
		InputValues   map[string]any    `json:"input_values,omitempty" jsonschema:"the module and provider-template inputs, keyed by field name. Build it from terrapod_catalog_item_form, not from the module's variables: a key that is not a form field is REFUSED 422 (it is the door a non-exposed module variable would otherwise be injected through), and a required field with no default left unsupplied is refused the same way. A field the item HID is not on the form and must not be sent -- its fixed value is wired for you"`
		VersionPin    string            `json:"version_pin,omitempty" jsonschema:"pin THIS instance to a module version, overriding the item's own pin. Omit to take the item's pin, or the module's latest uploaded version when the item floats"`
		AutoApply     bool              `json:"auto_apply,omitempty" jsonschema:"apply the first run without waiting for a human. DEFAULTS FALSE, which is the safe setting: the run plans and waits, and you confirm it with terrapod_catalog_instance_confirm after showing the plan. Setting this true creates real cloud resources with no review"`
		Labels        map[string]string `json:"labels,omitempty" jsonschema:"labels for the new workspace. Reserved keys are refused (422), and so is a label set that would pull the workspace into a variable set the caller cannot already reach -- provisioning needs only catalog use plus pool write, so an unchecked label here would be a path to another team's secrets (GHSA-49q6-pm68-3xgw)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_provision",
		Description: "Provision an INSTANCE from a service-catalog item: create a catalog-managed workspace with a generated configuration (the module call, the item's provider templates, the inputs you supply) and queue its first run. " +
			"THIS CREATES REAL CLOUD INFRASTRUCTURE and costs money. The run it queues is never plan-only — with `auto_apply` false (the default, and the safe setting) it plans and WAITS, which is the point at which to show the plan and ask; with `auto_apply` true it applies unreviewed. " +
			"CONFIRM IT WITH terrapod_catalog_instance_confirm, NOT terrapod_run_apply. An instance's workspace is clamped so that every non-platform-admin grant on it is capped at read, including for the person who provisioned it, so the workspace run API refuses them. The catalog surface is the instance's control plane. " +
			"Read terrapod_catalog_item_form first and build `input_values` from its fields; the call refuses an unknown key and a missing required one. Needs catalog `use` on the item AND write on the chosen agent pool, which must be one the item permits. " +
			"When the item declares an Ansible half, the generated configuration version also carries the module's `ansible-directory` subtree copied in at the same path, and the workspace is given that directory plus the item's default playbook and limit. Three refusals land here rather than at item-create time: the module has no uploaded version to copy from; the directory names the module root rather than a subdirectory; or the subtree holds no files, in which case the error names what the module does ship. " +
			"The instance's configuration and variables are thereafter catalog-managed and regenerated on each reconfigure, so editing them through the workspace tools is an affordance for a change the next reconfigure discards — the one exception is the workspace's ANSIBLE INVENTORY rows, which the provisioner may edit, though the module declares them and an apply reverts a hand-edited row. " +
			"Returns the instance; its id is the new workspace's UUID.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in provisionIn) (*mcp.CallToolResult, catalogInstanceOut, error) {
		switch {
		case in.CatalogItemID == "":
			return errText("catalog_item_id is required"), catalogInstanceOut{}, nil
		case in.Name == "":
			return errText("name is required"), catalogInstanceOut{}, nil
		case in.AgentPoolID == "":
			return errText("agent_pool_id is required: a catalog instance is always agent-executed"), catalogInstanceOut{}, nil
		}
		attrs := map[string]any{
			"name":          in.Name,
			"agent-pool-id": in.AgentPoolID,
			"auto-apply":    in.AutoApply,
		}
		if in.InputValues != nil {
			attrs["input-values"] = in.InputValues
		}
		if in.VersionPin != "" {
			attrs["version-pin"] = in.VersionPin
		}
		if in.Labels != nil {
			attrs["labels"] = in.Labels
		}
		inst, err := c.ProvisionCatalogItem(ctx, in.CatalogItemID, attrs)
		if err != nil {
			return errResult(err), catalogInstanceOut{}, nil
		}
		return nil, instanceOut(inst), nil
	})

	// ── terrapod_catalog_instance_list ───────────────────────────────
	type instanceListIn struct {
		CatalogItemID string `json:"catalog_item_id" jsonschema:"the catalog item whose instances to list (a bare UUID)"`
	}
	type instanceListOut struct {
		Count     int                  `json:"count"`
		Instances []catalogInstanceOut `json:"instances"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_list",
		Description: "List the instances provisioned from one service-catalog item — each a catalog-managed workspace, with its name, agent pool, version pin, owner and labels. " +
			"An instance's `id` IS its workspace UUID, so it is also addressable through the workspace tools as `ws-<id>`: use those to read its runs, state and drift, and the catalog instance tools to change it. " +
			"Every page is fetched, so the count is the whole set rather than the first page. Read it before deleting the item, which is refused while any instance is still active. " +
			"Needs catalog read on the item.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in instanceListIn) (*mcp.CallToolResult, instanceListOut, error) {
		if in.CatalogItemID == "" {
			return errText("catalog_item_id is required"), instanceListOut{}, nil
		}
		instances, err := c.ListAllCatalogInstances(ctx, in.CatalogItemID)
		if err != nil {
			return errResult(err), instanceListOut{}, nil
		}
		return nil, instanceListOut{Count: len(instances), Instances: instancesOut(instances)}, nil
	})

	// ── terrapod_catalog_instance_get ────────────────────────────────
	type instanceIDIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the instance's workspace id (ws-... or a bare UUID) -- a catalog instance IS a workspace, so its id is the workspace's"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_get",
		Description: "Get one catalog instance by its workspace id — its name, originating item, version pin, agent pool, labels, and `input-values`, the inputs it was last provisioned or reconfigured with. " +
			"`input-values` is the set to start from for a reconfigure, because that call REPLACES the stored values rather than merging into them. " +
			"This is the catalog view; terrapod_workspace_get on the same id gives the workspace view (runs, state, drift, lock). Needs catalog read on the ORIGINATING ITEM, which is a different permission from read on the workspace. " +
			"A 404 means there is no catalog instance there: either no such workspace, or a workspace that was never provisioned from an item.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in instanceIDIn) (*mcp.CallToolResult, catalogInstanceOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), catalogInstanceOut{}, nil
		}
		inst, err := c.GetCatalogInstance(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), catalogInstanceOut{}, nil
		}
		return nil, instanceOut(inst), nil
	})

	// ── terrapod_catalog_instance_reconfigure ────────────────────────
	//
	// `input_values` and `version_pin` are pointers because absent and empty
	// differ on both: an absent `input_values` keeps the stored set, and an
	// empty `version_pin` moves the instance from a pin to floating.
	type reconfigureIn struct {
		WorkspaceID string          `json:"workspace_id" jsonschema:"the instance's workspace id (ws-... or a bare UUID)"`
		InputValues *map[string]any `json:"input_values,omitempty" jsonschema:"REPLACE the instance's inputs with this WHOLE set -- it is not a patch. Read the current ones from terrapod_catalog_instance_get and send them back with your change applied: a required field left out is refused 422 rather than keeping its stored value, and a key that is not a form field is refused too. Omit the field entirely to keep the stored set unchanged, which is how a version-pin-only reconfigure is expressed"`
		VersionPin  *string         `json:"version_pin,omitempty" jsonschema:"pin this instance to a module version. OMIT to keep its current pin; pass EMPTY to make it FLOAT, which resolves to the module's latest uploaded version now and at every later reconfigure. So empty is a change of tracking mode rather than a cleared field"`
		AutoApply   bool            `json:"auto_apply,omitempty" jsonschema:"apply the resulting run without waiting for a human. DEFAULTS FALSE, which is the safe setting: the run plans and waits for terrapod_catalog_instance_confirm. Setting it true changes real infrastructure with no review"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_reconfigure",
		Description: "Reconfigure a catalog instance: regenerate its configuration from the current state of its item, apply new inputs and/or a new version pin, and queue a run. Returns the queued run's id and status. " +
			"THIS CHANGES REAL INFRASTRUCTURE. A changed input can force a resource to be replaced or removed, so treat it as an apply rather than an edit: with `auto_apply` false (the default) the run plans and waits — read it with terrapod_run_plan_json and show it before confirming with terrapod_catalog_instance_confirm. " +
			"`input_values` REPLACES the stored set rather than merging into it, so read terrapod_catalog_instance_get first and send the whole thing. Omitting it keeps the stored values, which is how a version-pin-only reconfigure is expressed. " +
			"It also re-renders everything the ITEM now says, not only what you pass here: a version bump, changed provider templates, changed form overrides and the Ansible half are all picked up at this point. That includes the OFF case — an item whose `configure-enabled` was turned off leaves this workspace with the configure operation GONE, rather than with a stale directory and a playbook it no longer has. " +
			"Confirm with the user before calling it. Needs catalog `use` on the originating item; refused 409 on an archived (already destroyed) instance.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in reconfigureIn) (*mcp.CallToolResult, *terrapod.CatalogRunRef, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		attrs := map[string]any{"auto-apply": in.AutoApply}
		if in.InputValues != nil {
			attrs["input-values"] = *in.InputValues
		}
		if in.VersionPin != nil {
			attrs["version-pin"] = *in.VersionPin
		}
		run, err := c.ReconfigureCatalogInstance(ctx, in.WorkspaceID, attrs)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_catalog_instance_destroy ────────────────────────────
	type destroyIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the instance's workspace id (ws-... or a bare UUID)"`
		AutoApply   bool   `json:"auto_apply,omitempty" jsonschema:"tear the infrastructure down without waiting for a human. DEFAULTS FALSE, which is the safe setting: the destroy PLANS and waits for terrapod_catalog_instance_confirm, so what it would remove can be read first. Setting it true destroys real infrastructure immediately and irreversibly"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_destroy",
		Description: "Queue a DESTROY run for a catalog instance: tear down the infrastructure it provisioned and, on a successful apply, archive its workspace. Returns the queued run's id and status. " +
			"IRREVERSIBLE once applied. With `auto_apply` false (the default) it plans and waits — read the destroy plan with terrapod_run_plan_json, show what it would remove, and only then confirm with terrapod_catalog_instance_confirm. Always get the user's explicit approval first. " +
			"This is the RECOMMENDED teardown, because it reclaims the infrastructure; terrapod_catalog_instance_orphan is the other one and abandons it still running. " +
			"Needs catalog `use` on the originating item; refused 409 on an instance already archived.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in destroyIn) (*mcp.CallToolResult, *terrapod.CatalogRunRef, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		run, err := c.DestroyCatalogInstance(ctx, in.WorkspaceID, map[string]any{"auto-apply": in.AutoApply})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_catalog_instance_confirm ────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_confirm",
		Description: "Confirm a catalog instance's pending PLANNED run so it applies — this changes real infrastructure, and for a destroy it is irreversible. Only call after the user has reviewed the plan and explicitly approved it. " +
			"This is the catalog surface's own apply, and it exists because terrapod_run_apply cannot do the job: an instance's workspace is clamped so every non-platform-admin grant on it is capped at read, so the person who provisioned it cannot confirm through the workspace run API. " +
			"It acts on the instance's LATEST run, and only when that run is a catalog one awaiting confirmation — a speculative module-impact plan on the same workspace is deliberately not confirmable here, or write access to a module would become apply access to every instance consuming it. A 409 means there is no such run. " +
			"Needs catalog `use` on the originating item. Returns the run's id and status.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in instanceIDIn) (*mcp.CallToolResult, *terrapod.CatalogRunRef, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		run, err := c.ConfirmCatalogInstanceRun(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_catalog_instance_discard ────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_discard",
		Description: "Discard a catalog instance's pending PLANNED run without applying it, so nothing changes. The catalog-surface counterpart of terrapod_catalog_instance_confirm, and it exists for the same reason: the workspace clamp blocks the provisioner from the workspace run API. " +
			"Use it when a provision, reconfigure or destroy plan should not proceed. Discarding a PROVISION's first run leaves the workspace in place with nothing applied — the instance exists and tracks no infrastructure, so either reconfigure it to try again or remove it. " +
			"Acts on the instance's latest run, and only when it is a catalog run awaiting confirmation; a 409 means there is no such run. Needs catalog `use` on the originating item.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in instanceIDIn) (*mcp.CallToolResult, *terrapod.CatalogRunRef, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), nil, nil
		}
		run, err := c.DiscardCatalogInstanceRun(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, run, nil
	})

	// ── terrapod_catalog_instance_orphan ─────────────────────────────
	//
	// Its own verb rather than a flag on a delete tool, because the API itself
	// refuses to orphan without an explicit `?orphan=true` — there is no way to
	// do this by accident, and the tool should not reintroduce one.
	type orphanOut struct {
		Orphaned                bool   `json:"orphaned"`
		WorkspaceID             string `json:"workspace_id"`
		InfrastructureAbandoned bool   `json:"infrastructure_abandoned"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_catalog_instance_orphan",
		Description: "ABANDON a catalog instance: delete its workspace record while LEAVING ITS INFRASTRUCTURE RUNNING, untracked by Terrapod. " +
			"This is the discouraged escape hatch, not a teardown. Nothing is destroyed and nothing is reclaimed — the resources keep running and keep costing money, with no state, no plan and no record of what created them, so bringing them back under management means importing each one by hand. " +
			"The recommended teardown is terrapod_catalog_instance_destroy, which reclaims the infrastructure. Reach for this one only when the resources must deliberately outlive Terrapod's management of them, and only with the user's explicit, informed approval — say plainly that the infrastructure will still exist afterwards. " +
			"Needs catalog `admin` on the originating item, a higher bar than the `use` that destroys it.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in instanceIDIn) (*mcp.CallToolResult, orphanOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), orphanOut{}, nil
		}
		if err := c.OrphanCatalogInstance(ctx, in.WorkspaceID); err != nil {
			return errResult(err), orphanOut{}, nil
		}
		return nil, orphanOut{Orphaned: true, WorkspaceID: in.WorkspaceID, InfrastructureAbandoned: true}, nil
	})
}
