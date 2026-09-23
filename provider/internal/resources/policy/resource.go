// Package policy implements the terrapod_policy resource (#1765).
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "policies"
//	Create:  POST   /api/terrapod/v1/policy-sets/{set}/policies
//	Read:    GET    /api/terrapod/v1/policy-sets/{set}   (policies are embedded)
//	Update:  PATCH  /api/terrapod/v1/policies/{id}
//	Delete:  DELETE /api/terrapod/v1/policies/{id}
//
// One Rego document inside a policy set, following the parent-and-children
// shape the repo already uses for `terrapod_variable_set` /
// `terrapod_variable_set_variable`.
//
// **There is no GET for a single policy.** The server never grew one, because
// the admin UI reads a set and its policies together; the SDK's `GetPolicy`
// fetches the set and picks the policy out, and returns NotFound when the set
// no longer holds it. That distinction is what lets Read drop a
// deleted-elsewhere policy from state instead of erroring.
//
// **Only inline sets accept these.** A VCS-sourced set manages its policies
// from the linked repository, and the server answers 409 — surfaced as-is
// rather than pre-empted here, because whether a set is VCS-backed can change
// between plan and apply.
package policy

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	"github.com/mattrobinsonsre/terrapod/provider/internal/ids"
)

var (
	_ resource.Resource                = &policyResource{}
	_ resource.ResourceWithImportState = &policyResource{}
)

type policyModel struct {
	ID          types.String `tfsdk:"id"`
	PolicySetID types.String `tfsdk:"policy_set_id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	Rego        types.String `tfsdk:"rego"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

type policyResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource {
	return &policyResource{}
}

func (r *policyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_policy"
}

func (r *policyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages one Rego policy inside an inline OPA policy set.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The policy's id.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"policy_set_id": schema.StringAttribute{
				Description: "The policy set this policy belongs to. Changing it forces a new resource — a policy is created under its set and the server offers no way to move one.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"name": schema.StringAttribute{
				Description: "The policy's name, unique within its set.",
				Required:    true,
			},
			"description": schema.StringAttribute{
				Description: "Human-readable description of what the policy enforces.",
				Optional:    true,
			},
			"rego": schema.StringAttribute{
				Description: "The Rego document. Must declare `package terrapod` and express violations through a `deny` set. Rego v1 syntax (`if` / `contains`); the server runs `opa check` and rejects a syntax error at apply time rather than at run time.",
				Required:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "When the policy was created (RFC3339).",
				Computed:    true,
			},
			"updated_at": schema.StringAttribute{
				Description: "When the policy was last updated (RFC3339).",
				Computed:    true,
			},
		},
	}
}

func (r *policyResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError(
			"Unexpected provider data type",
			fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData),
		)
		return
	}
	r.client = c

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *policyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m policyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	p, err := r.tc.AddPolicy(ctx, m.PolicySetID.ValueString(), terrapod.AddPolicyRequest{
		Name:        m.Name.ValueString(),
		Description: m.Description.ValueString(),
		Rego:        m.Rego.ValueString(),
	})
	if err != nil {
		resp.Diagnostics.AddError("Unable to add policy", err.Error())
		return
	}

	readIntoModel(p, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m policyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	p, err := r.tc.GetPolicy(ctx, m.PolicySetID.ValueString(), m.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			// Either the policy or its whole set is gone. Both mean the same
			// thing to Terraform: this no longer exists, so recreate it.
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Unable to read policy", err.Error())
		return
	}

	readIntoModel(p, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policyResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m policyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	name := m.Name.ValueString()
	desc := m.Description.ValueString()
	rego := m.Rego.ValueString()

	p, err := r.tc.UpdatePolicy(ctx, m.ID.ValueString(), terrapod.UpdatePolicyRequest{
		Name:        &name,
		Description: &desc,
		Rego:        &rego,
	})
	if err != nil {
		resp.Diagnostics.AddError("Unable to update policy", err.Error())
		return
	}

	readIntoModel(p, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *policyResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m policyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}

	if err := r.tc.DeletePolicy(ctx, m.ID.ValueString()); err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			return
		}
		resp.Diagnostics.AddError("Unable to delete policy", err.Error())
	}
}

// ImportState takes "<policy-set-id>/<policy-id>", because a policy cannot be
// read without knowing its set — there is no GET for one on its own.
func (r *policyResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	setID, policyID, ok := splitImportID(req.ID)
	if !ok {
		resp.Diagnostics.AddError(
			"Unexpected import identifier",
			fmt.Sprintf(
				"Expected \"<policy-set-id>/<policy-id>\", got %q. A policy is read through its "+
					"set, so the set's id is needed to find it.", req.ID,
			),
		)
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("policy_set_id"), setID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), policyID)...)
}

// splitImportID cuts on the LAST separator. Ids carry typed prefixes and
// uuids, neither of which contains "/", but splitting from the right keeps
// this correct if one ever does.
func splitImportID(s string) (setID, policyID string, ok bool) {
	for i := len(s) - 1; i >= 0; i-- {
		if s[i] == '/' {
			setID, policyID = s[:i], s[i+1:]
			return setID, policyID, setID != "" && policyID != ""
		}
	}
	return "", "", false
}

func readIntoModel(p *terrapod.Policy, m *policyModel) {
	m.ID = types.StringValue(p.ID)
	m.Name = types.StringValue(p.Name)
	m.Rego = types.StringValue(p.Rego)
	m.CreatedAt = types.StringValue(p.CreatedAt)
	m.UpdatedAt = types.StringValue(p.UpdatedAt)

	// The relationship carries the set's prefixed id while a configuration may
	// have written either form; keep what it wrote (#1748).
	if p.PolicySetID != "" {
		m.PolicySetID = ids.Keep(m.PolicySetID, p.PolicySetID, "polset-")
	}

	// An unset optional answers as "", and storing that would plan forever.
	if p.Description == "" && (m.Description.IsNull() || m.Description.IsUnknown()) {
		m.Description = types.StringNull()
	} else {
		m.Description = types.StringValue(p.Description)
	}
}
