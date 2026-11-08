// Package inventory_resolved implements the terrapod_inventory_resolved data
// source (#1967): what a workspace's ansible inventory resolves to, once the
// rows declared through Terrapod and the bound VCS directory have been merged.
//
// It is a data source and not an attribute on a resource because the merged
// view is derived: a data source has no round-trip requirement, so it can carry
// a value the server computes and nothing in the configuration owns. That is
// the same reasoning as terrapod_oidc_audience_defaults — the resource tells
// you what you declared, this tells you what it adds up to.
//
// ANSIBLE produces this, not Terrapod. The server renders the declared rows to
// one YAML document and runs `ansible-inventory --list` over that and the VCS
// directory, so the precedence rules, the group DAG, the derivation of `all`
// and `ungrouped`, and `--limit` expansion are all ansible's.
//
// It is LIVE and FAILS CLOSED. There is no cached shape and no freshness to
// reason about (dynamic inventory was declined, #1970, so every source is
// static), and a resolution error is an error rather than an empty host set: a
// configure silently targeting too little is worse than no answer at all, and
// unlike a policy gate there is no later evaluation to catch it.
package inventory_resolved

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &inventoryResolvedDataSource{}

type inventoryResolvedDataSource struct {
	tc *terrapod.Client
	// defaultWorkspace is the provider's `workspace` (#2036); "" when unset.
	defaultWorkspace string
}

type inventoryResolvedModel struct {
	WorkspaceID   types.String `tfsdk:"workspace_id"`
	Limit         types.String `tfsdk:"limit"`
	Provenance    types.Bool   `tfsdk:"provenance"`
	Hosts         types.Map    `tfsdk:"hosts"`
	VarProvenance types.Map    `tfsdk:"var_provenance"`
	Groups        types.Map    `tfsdk:"groups"`
	GroupChildren types.Map    `tfsdk:"group_children"`
	HostCount     types.Int64  `tfsdk:"host_count"`
	GroupCount    types.Int64  `tfsdk:"group_count"`
}

// NewDataSource returns the terrapod_inventory_resolved data source.
func NewDataSource() datasource.DataSource {
	return &inventoryResolvedDataSource{}
}

func (d *inventoryResolvedDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_inventory_resolved"
}

func (d *inventoryResolvedDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "What a workspace's ansible inventory resolves to: the hosts, groups, nesting and " +
			"variables that the rows declared through Terrapod and the bound VCS directory add up to. Ansible " +
			"produces it — the server renders the declared rows to one document and runs " +
			"`ansible-inventory --list` over that and the VCS source — so the precedence rules, the group DAG " +
			"and the derivation of `all` and `ungrouped` are ansible's. Resolved on every read, with nothing " +
			"cached that a caller has to reason about, and a resolution failure is an error rather than an " +
			"empty host set. See docs/ansible-inventory.md.",
		Attributes: map[string]schema.Attribute{
			"workspace_id": schema.StringAttribute{
				Description: "The workspace whose inventory to resolve (e.g. \"ws-abc123\"). Defaults to " +
					"the provider's `workspace` attribute or TERRAPOD_WORKSPACE, which a run exports from " +
					"its own workspace in both phases — so inside a run this can be left out.",
				Optional: true,
				Computed: true,
			},
			"limit": schema.StringAttribute{
				Description: "An optional `--limit` pattern, expanded by ansible itself, so it carries every " +
					"term ansible does: a group name, a host name, a glob, `!exclusions`, `&intersections` and " +
					"`~regex`. It expands THROUGH nesting, which `groups` does not, so asking with a limit is " +
					"the authoritative answer to \"what would this target\".",
				Optional: true,
			},
			"provenance": schema.BoolAttribute{
				Description: "Ask the server to attribute every host variable to the level that set it, " +
					"populating `var_provenance`. Off by default because it costs a second ansible " +
					"invocation (`--graph --vars`), and it composes with `limit` — \"these are the hosts a " +
					"configure would touch, and this is where each of their variables came from\" is one " +
					"question.",
				Optional: true,
			},
			"hosts": schema.MapAttribute{
				Description: "Every host, keyed on its inventory hostname, to its resolved variables. " +
					"Exhaustive — a host with no variables is present with an empty map, deliberately unlike " +
					"ansible's own `_meta.hostvars`, where such a host is omitted entirely and enumerating a " +
					"host set from it loses hosts silently. A variable's value is given verbatim when it is a " +
					"string and JSON-encoded otherwise, because a Terraform map is homogeneous.",
				Computed:    true,
				ElementType: types.MapType{ElemType: types.StringType},
			},
			"var_provenance": schema.MapAttribute{
				Description: "Per host, per variable, WHERE the merged value came from — the answer to \"why " +
					"does this host have this value\" and \"which group do I change\", neither of which is " +
					"derivable from `hosts`, because the merge has already discarded what it overrode. " +
					"Empty unless `provenance` is set. `origin` is the level that WON: a group by NAME, or " +
					"`here` for a value the host itself set. `chain` is every level that set the key, " +
					"shallowest group first, in ansible's own precedence order — so everything `origin` beat. " +
					"Values are rendered as `hosts` renders them: a string verbatim, anything else " +
					"JSON-encoded. ANSIBLE attributes these, not Terrapod: the levels come from " +
					"`ansible-inventory --graph --vars`, and the server does the walk so no consumer " +
					"re-implements a second precedence rule that could diverge from ansible silently.",
				Computed:    true,
				ElementType: types.MapType{ElemType: types.ObjectType{AttrTypes: varOriginAttrTypes()}},
			},
			"groups": schema.MapAttribute{
				Description: "Each group's DIRECT member hosts, keyed on group name. Ansible does not flatten " +
					"nesting into a group's host list, so a parent whose members all arrive through a child " +
					"reports NONE of its own: do not read an empty list as \"targets nothing\". Read " +
					"`group_children` for the structure, or ask with a `limit`, which expands through nesting.",
				Computed:    true,
				ElementType: types.ListType{ElemType: types.StringType},
			},
			"group_children": schema.MapAttribute{
				Description: "The nesting `groups` does not carry: each parent group name to the names of the " +
					"groups nested inside it.",
				Computed:    true,
				ElementType: types.ListType{ElemType: types.StringType},
			},
			"host_count": schema.Int64Attribute{
				Description: "How many hosts the inventory resolves to.",
				Computed:    true,
			},
			"group_count": schema.Int64Attribute{
				Description: "How many groups the inventory resolves to.",
				Computed:    true,
			},
		},
	}
}

func (d *inventoryResolvedDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type",
			fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
	d.defaultWorkspace = c.DefaultWorkspace
}

func (d *inventoryResolvedDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config inventoryResolvedModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// A data source has no plan/apply comparison, so the default is resolved here
	// rather than in a plan modifier -- and written back into the model, so state
	// records which workspace was actually read (#2036).
	workspaceID := config.WorkspaceID.ValueString()
	if config.WorkspaceID.IsNull() || config.WorkspaceID.IsUnknown() {
		workspaceID = d.defaultWorkspace
	}
	if workspaceID == "" {
		resp.Diagnostics.AddError("Missing workspace",
			"Set workspace_id on this data source, or the provider's `workspace` attribute, or "+
				"TERRAPOD_WORKSPACE. Inside a Terrapod run the last is exported for you.")
		return
	}
	config.WorkspaceID = types.StringValue(workspaceID)
	limit := ""
	if !config.Limit.IsNull() && !config.Limit.IsUnknown() {
		limit = config.Limit.ValueString()
	}

	// One call for every combination, including both parameters together, which
	// neither of the older single-option methods can ask for. An empty options
	// struct sends no query string at all, so asking for neither is byte-for-byte
	// the request this data source made before `provenance` existed.
	resolved, err := d.tc.GetResolvedInventoryWithOptions(ctx, workspaceID,
		terrapod.ResolvedInventoryOptions{
			Limit:      limit,
			Provenance: !config.Provenance.IsNull() && config.Provenance.ValueBool(),
		})
	if err != nil {
		// Fails closed, on purpose. Reporting an empty inventory instead would
		// let a configure target too little while looking like it worked.
		resp.Diagnostics.AddError("Failed to resolve the workspace's inventory", err.Error())
		return
	}

	resp.Diagnostics.Append(readIntoModel(ctx, resolved, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}

// readIntoModel projects the resolution into the model.
//
// Every collection is an empty map rather than null when the inventory holds
// nothing, so a configuration indexing into one gets "no such key" instead of
// an error about a null value.
func readIntoModel(ctx context.Context, r *terrapod.ResolvedInventory, m *inventoryResolvedModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.HostCount = types.Int64Value(int64(r.HostCount))
	m.GroupCount = types.Int64Value(int64(r.GroupCount))

	hosts, d := types.MapValueFrom(ctx,
		types.MapType{ElemType: types.StringType}, flattenHosts(r.Hosts))
	diags.Append(d...)
	m.Hosts = hosts

	groups, d := types.MapValueFrom(ctx,
		types.ListType{ElemType: types.StringType}, orEmptyLists(r.Groups))
	diags.Append(d...)
	m.Groups = groups

	children, d := types.MapValueFrom(ctx,
		types.ListType{ElemType: types.StringType}, orEmptyLists(r.GroupChildren))
	diags.Append(d...)
	m.GroupChildren = children

	prov, d := flattenProvenance(r.VarProvenance)
	diags.Append(d...)
	m.VarProvenance = prov

	return diags
}

// varOriginAttrTypes is the shape of one attributed variable, declared once so
// the schema and the projection cannot disagree about it — a mismatch between
// those two is a runtime type error, not a compile failure.
func varOriginAttrTypes() map[string]attr.Type {
	return map[string]attr.Type{
		"value":  types.StringType,
		"origin": types.StringType,
		"chain":  types.ListType{ElemType: types.ObjectType{AttrTypes: chainLinkAttrTypes()}},
	}
}

// chainLinkAttrTypes is one link in a precedence chain: the level, and the value
// it set there.
func chainLinkAttrTypes() map[string]attr.Type {
	return map[string]attr.Type{
		"level": types.StringType,
		"value": types.StringType,
	}
}

// flattenProvenance projects the attribution, host by host.
//
// An EMPTY map when nothing was asked for, never null, so a configuration that
// forgot `provenance = true` reads "no such key" rather than erroring about a
// null collection — the same reasoning every other collection here follows. The
// cost is that "not asked" and "asked, and no host carries a variable" read
// alike; both mean there is nothing to read, and the attribute's description
// names the remedy.
//
// A chain link's value is rendered through renderValue like every other
// inventory value. The server stringifies it on the way out — the levels are
// parsed from ansible's own text — but the SDK types it as arbitrary JSON, and
// rendering it the same way as `hosts` is correct either way rather than relying
// on which of the two is true today.
func flattenProvenance(in map[string]map[string]terrapod.VarOrigin) (types.Map, diag.Diagnostics) {
	var diags diag.Diagnostics
	originType := types.ObjectType{AttrTypes: varOriginAttrTypes()}
	perHostType := types.MapType{ElemType: originType}
	linkType := types.ObjectType{AttrTypes: chainLinkAttrTypes()}

	hosts := make(map[string]attr.Value, len(in))
	for host, vars := range in {
		attributed := make(map[string]attr.Value, len(vars))
		for key, origin := range vars {
			links := make([]attr.Value, 0, len(origin.Chain))
			for _, link := range origin.Chain {
				obj, d := types.ObjectValue(chainLinkAttrTypes(), map[string]attr.Value{
					"level": types.StringValue(link.Level),
					"value": types.StringValue(renderValue(link.Value)),
				})
				diags.Append(d...)
				links = append(links, obj)
			}
			chain, d := types.ListValue(linkType, links)
			diags.Append(d...)
			obj, d := types.ObjectValue(varOriginAttrTypes(), map[string]attr.Value{
				"value":  types.StringValue(renderValue(origin.Value)),
				"origin": types.StringValue(origin.Origin),
				"chain":  chain,
			})
			diags.Append(d...)
			attributed[key] = obj
		}
		perHost, d := types.MapValue(originType, attributed)
		diags.Append(d...)
		hosts[host] = perHost
	}

	out, d := types.MapValue(perHostType, hosts)
	diags.Append(d...)
	return out, diags
}

// flattenHosts renders each host's variables as strings.
//
// A Terraform map is homogeneous and an inventory variable is not: a value may
// be a string, a number, a bool, a list or an object. So a string is given
// verbatim and anything else is JSON-encoded — the same convention the catalog
// item interface uses for a module input's default, and the one the
// `structured` flag on a declared variable already expresses.
func flattenHosts(hosts map[string]map[string]any) map[string]map[string]string {
	out := make(map[string]map[string]string, len(hosts))
	for name, vars := range hosts {
		rendered := make(map[string]string, len(vars))
		for key, raw := range vars {
			rendered[key] = renderValue(raw)
		}
		out[name] = rendered
	}
	return out
}

func renderValue(v any) string {
	switch x := v.(type) {
	case nil:
		// An explicit YAML null. "" is the closest honest rendering: a
		// Terraform map cannot hold a null element alongside strings.
		return ""
	case string:
		return x
	default:
		b, err := json.Marshal(x)
		if err != nil {
			return fmt.Sprintf("%v", x)
		}
		return string(b)
	}
}

// orEmptyLists replaces a nil list with an empty one, so a group that resolves
// to no direct members reads as an empty list rather than null.
func orEmptyLists(in map[string][]string) map[string][]string {
	out := make(map[string][]string, len(in))
	for k, v := range in {
		if v == nil {
			v = []string{}
		}
		out[k] = v
	}
	return out
}
