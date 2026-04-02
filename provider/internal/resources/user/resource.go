// Package user implements the terrapod_user resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "users"
//	ID: email (string, not a generated ID)
//	Create:  POST   /api/v2/organizations/default/users
//	Read:    GET    /api/v2/users/{email}
//	Update:  PATCH  /api/v2/users/{email}
//	Delete:  DELETE /api/v2/users/{email}
//
// Attribute mapping (JSON:API attribute -> Terraform schema attribute):
//
//	"email"        -> email        (string, required, forces new — used as ID)
//	"display-name" -> display_name (string, optional)
//	"is-active"    -> is_active    (bool,   optional, default true)
//	"password"     -> password     (string, optional, sensitive, write-only)
//
// Read-only attributes:
//
//	"has-password"  -> has_password  (bool,   computed)
//	"last-login-at" -> last_login_at (string, computed)
//	"created-at"    -> created_at    (string, computed)
//	"updated-at"    -> updated_at    (string, computed)
//
// Import: by email (used directly as the resource ID).
package user

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &userResource{}
	_ resource.ResourceWithImportState = &userResource{}
)

// userModel maps the Terraform schema to Go types.
type userModel struct {
	ID types.String `tfsdk:"id"`

	// Writable attributes
	Email       types.String `tfsdk:"email"`
	DisplayName types.String `tfsdk:"display_name"`
	IsActive    types.Bool   `tfsdk:"is_active"`
	Password    types.String `tfsdk:"password"`

	// Read-only attributes
	HasPassword types.Bool   `tfsdk:"has_password"`
	LastLoginAt types.String `tfsdk:"last_login_at"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

type userResource struct {
	client *client.Client
}

// NewResource returns a new user resource.
func NewResource() resource.Resource {
	return &userResource{}
}

func (r *userResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_user"
}

func (r *userResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a Terrapod user.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Description: "The user ID (email address).",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"email": schema.StringAttribute{
				Description: "The user's email address. Used as the unique identifier. Changing this forces a new resource.",
				Required:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"display_name": schema.StringAttribute{
				Description: "The user's display name.",
				Optional:    true,
				Computed:    true,
			},
			"is_active": schema.BoolAttribute{
				Description: "Whether the user account is active.",
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(true),
			},
			"password": schema.StringAttribute{
				Description: "The user's password. Write-only; never returned by the API.",
				Optional:    true,
				Sensitive:   true,
			},

			// Read-only
			"has_password": schema.BoolAttribute{
				Description: "Whether the user has a password set.",
				Computed:    true,
			},
			"last_login_at": schema.StringAttribute{
				Description: "Timestamp of the user's last login.",
				Computed:    true,
			},
			"created_at": schema.StringAttribute{
				Description: "Creation timestamp.",
				Computed:    true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"updated_at": schema.StringAttribute{
				Description: "Last update timestamp.",
				Computed:    true,
			},
		},
	}
}

func (r *userResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *userResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan userModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildUserAttrs(&plan)

	body, err := client.MarshalResource("users", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/v2/organizations/default/users", body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to create user", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	readUserIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *userResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/users/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read user", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	// Preserve the write-only password from state (API never returns it).
	password := state.Password

	readUserIntoModel(res, &state)
	state.Password = password
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *userResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan userModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var state userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildUserAttrs(&plan)

	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "users", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Failed to marshal request", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/users/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Failed to update user", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Failed to parse response", err.Error())
		return
	}

	// Preserve the write-only password from plan.
	password := plan.Password

	readUserIntoModel(res, &plan)
	plan.Password = password
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *userResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/users/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Failed to delete user", err.Error())
	}
}

func (r *userResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	// Import by email — the email IS the ID.
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}

// buildUserAttrs converts the Terraform model into JSON:API attributes.
func buildUserAttrs(m *userModel) map[string]any {
	attrs := map[string]any{
		"email": m.Email.ValueString(),
	}

	if !m.DisplayName.IsNull() && !m.DisplayName.IsUnknown() {
		attrs["display-name"] = m.DisplayName.ValueString()
	}
	if !m.IsActive.IsNull() && !m.IsActive.IsUnknown() {
		attrs["is-active"] = m.IsActive.ValueBool()
	}
	if !m.Password.IsNull() && !m.Password.IsUnknown() {
		attrs["password"] = m.Password.ValueString()
	}

	return attrs
}

// readUserIntoModel populates the Terraform model from a JSON:API resource.
func readUserIntoModel(res *client.Resource, m *userModel) {
	// The user ID is the email address.
	m.ID = types.StringValue(res.ID)
	m.Email = types.StringValue(client.GetStringAttr(res, "email"))

	if v := client.GetStringAttr(res, "display-name"); v != "" {
		m.DisplayName = types.StringValue(v)
	} else {
		m.DisplayName = types.StringNull()
	}

	m.IsActive = types.BoolValue(client.GetBoolAttr(res, "is-active"))
	m.HasPassword = types.BoolValue(client.GetBoolAttr(res, "has-password"))

	// Password is never returned by the API — leave it untouched (caller preserves it).

	if v := client.GetStringAttr(res, "last-login-at"); v != "" {
		m.LastLoginAt = types.StringValue(v)
	} else {
		m.LastLoginAt = types.StringNull()
	}

	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))
}
