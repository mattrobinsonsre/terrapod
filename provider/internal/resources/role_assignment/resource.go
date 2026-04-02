// Package role_assignment implements the terrapod_role_assignment resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "role-assignments"
//	ID: composite — provider_name/email/role_name
//
//	Create: Read-modify-write via:
//	  1. GET  /api/v2/role-assignments                        — list all, filter by provider+email
//	  2. PUT  /api/v2/role-assignments                        — replace-all semantics for (provider, email)
//	     Body: {"data": {"attributes": {"provider-name": "...", "email": "...", "roles": ["admin", "custom"]}}}
//
//	Read:   GET    /api/v2/role-assignments                   — list all, find matching entry
//	Delete: DELETE /api/v2/role-assignments/{provider}/{email}/{role}
//
// Each Terraform resource instance represents ONE (provider, email, role)
// triple. Create adds the role to the user's existing set; delete removes
// only that single role.
//
// Attribute mapping (JSON:API attribute → Terraform schema attribute):
//
//	"provider-name" → provider_name (string, required, forces new)
//	"email"         → email         (string, required, forces new)
//	"role-name"     → role_name     (string, required, forces new)
//
// Read-only attributes:
//
//	"created-at" → created_at (string, computed)
//
// Import: provider_name/email/role_name
package role_assignment

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &roleAssignmentResource{}
	_ resource.ResourceWithImportState = &roleAssignmentResource{}
)

// roleAssignmentModel maps the Terraform schema to Go types.
type roleAssignmentModel struct {
	ID types.String `tfsdk:"id"`

	// All three fields are immutable — changing any forces replacement.
	ProviderName types.String `tfsdk:"provider_name"`
	Email        types.String `tfsdk:"email"`
	RoleName     types.String `tfsdk:"role_name"`

	// Read-only
	CreatedAt types.String `tfsdk:"created_at"`
}

type roleAssignmentResource struct {
	client *client.Client
}

// NewResource returns a new role assignment resource.
func NewResource() resource.Resource {
	return &roleAssignmentResource{}
}

func (r *roleAssignmentResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_role_assignment"
}

func (r *roleAssignmentResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Assigns a single role to a user identity in Terrapod. Each resource instance represents one (provider, email, role) binding.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "Composite ID: provider_name/email/role_name.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"provider_name": schema.StringAttribute{
				Description: "Authentication provider name (e.g. 'local', 'auth0'). Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"email": schema.StringAttribute{
				Description: "User email address. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"role_name": schema.StringAttribute{
				Description: "Role name to assign. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},

			// Read-only
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
		},
	}
}

func (r *roleAssignmentResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	r.client = c
}

func (r *roleAssignmentResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan roleAssignmentModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	providerName := plan.ProviderName.ValueString()
	email := plan.Email.ValueString()
	roleName := plan.RoleName.ValueString()

	// Read the current roles for this (provider, email) pair.
	currentRoles, err := r.listRolesForIdentity(ctx, providerName, email)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read current roles", err.Error())
		return
	}

	// Add the new role if not already present.
	found := false
	for _, rn := range currentRoles {
		if rn == roleName {
			found = true
			break
		}
	}
	if !found {
		currentRoles = append(currentRoles, roleName)
	}

	// PUT the full role list.
	if err := r.putRoles(ctx, providerName, email, currentRoles); err != nil {
		resp.Diagnostics.AddError("Failed to create role assignment", err.Error())
		return
	}

	// Read back to get created_at.
	assignment, err := r.findAssignment(ctx, providerName, email, roleName)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read back assignment", err.Error())
		return
	}
	if assignment == nil {
		resp.Diagnostics.AddError("Assignment not found after create", fmt.Sprintf("Could not find assignment %s/%s/%s", providerName, email, roleName))
		return
	}

	plan.ID = types.StringValue(providerName + "/" + email + "/" + roleName)
	plan.CreatedAt = types.StringValue(assignment.createdAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *roleAssignmentResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state roleAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	providerName := state.ProviderName.ValueString()
	email := state.Email.ValueString()
	roleName := state.RoleName.ValueString()

	assignment, err := r.findAssignment(ctx, providerName, email, roleName)
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read role assignment", err.Error())
		return
	}
	if assignment == nil {
		resp.State.RemoveResource(ctx)
		return
	}

	state.ID = types.StringValue(providerName + "/" + email + "/" + roleName)
	state.CreatedAt = types.StringValue(assignment.createdAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *roleAssignmentResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	// All attributes force replacement — Update should never be called.
	resp.Diagnostics.AddError("Update not supported", "Role assignments are immutable. Delete and recreate instead.")
}

func (r *roleAssignmentResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state roleAssignmentModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	deletePath := fmt.Sprintf("/api/v2/role-assignments/%s/%s/%s",
		state.ProviderName.ValueString(),
		state.Email.ValueString(),
		state.RoleName.ValueString(),
	)

	err := r.client.Delete(ctx, deletePath)
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete role assignment", err.Error())
	}
}

func (r *roleAssignmentResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import format: provider_name/email/role_name
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: provider_name/email/role_name")
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("provider_name"), parts[0])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("email"), parts[1])...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("role_name"), parts[2])...)
}

// assignmentData holds the fields we extract from a role-assignments list entry.
type assignmentData struct {
	providerName string
	email        string
	roleName     string
	createdAt    string
}

// listRolesForIdentity fetches all role assignments and returns the role names
// for the given (provider, email) pair.
func (r *roleAssignmentResource) listRolesForIdentity(ctx context.Context, providerName, email string) ([]string, error) {
	data, err := r.client.Get(ctx, "/api/v2/role-assignments")
	if err != nil {
		return nil, err
	}

	assignments, err := parseAssignmentList(data)
	if err != nil {
		return nil, err
	}

	var roles []string
	for _, a := range assignments {
		if a.providerName == providerName && a.email == email {
			roles = append(roles, a.roleName)
		}
	}
	return roles, nil
}

// findAssignment looks up a specific (provider, email, role) triple in the
// assignment list.
func (r *roleAssignmentResource) findAssignment(ctx context.Context, providerName, email, roleName string) (*assignmentData, error) {
	data, err := r.client.Get(ctx, "/api/v2/role-assignments")
	if err != nil {
		return nil, err
	}

	assignments, err := parseAssignmentList(data)
	if err != nil {
		return nil, err
	}

	for _, a := range assignments {
		if a.providerName == providerName && a.email == email && a.roleName == roleName {
			return &a, nil
		}
	}
	return nil, nil
}

// putRoles sends a PUT to replace all roles for a (provider, email) pair.
func (r *roleAssignmentResource) putRoles(ctx context.Context, providerName, email string, roles []string) error {
	body, err := json.Marshal(map[string]any{
		"data": map[string]any{
			"attributes": map[string]any{
				"provider-name": providerName,
				"email":         email,
				"roles":         roles,
			},
		},
	})
	if err != nil {
		return fmt.Errorf("marshalling PUT body: %w", err)
	}

	_, err = r.client.Put(ctx, "/api/v2/role-assignments", body)
	return err
}

// parseAssignmentList parses the GET /api/v2/role-assignments response.
// The response has {"data": [{"type": "role-assignments", "attributes": {...}}, ...]}.
func parseAssignmentList(data []byte) ([]assignmentData, error) {
	var doc struct {
		Data []struct {
			Type       string         `json:"type"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}

	assignments := make([]assignmentData, 0, len(doc.Data))
	for _, item := range doc.Data {
		a := assignmentData{
			providerName: getStringFromMap(item.Attributes, "provider-name"),
			email:        getStringFromMap(item.Attributes, "email"),
			roleName:     getStringFromMap(item.Attributes, "role-name"),
			createdAt:    getStringFromMap(item.Attributes, "created-at"),
		}
		assignments = append(assignments, a)
	}
	return assignments, nil
}

// getStringFromMap reads a string value from a map[string]any.
func getStringFromMap(m map[string]any, key string) string {
	v, ok := m[key]
	if !ok || v == nil {
		return ""
	}
	s, ok := v.(string)
	if !ok {
		return fmt.Sprintf("%v", v)
	}
	return s
}
