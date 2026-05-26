// Package user implements the terrapod_user resource.
//
// API Contract (Terrapod API <-> Terraform Provider):
//
//	JSON:API type: "users"
//	ID: email (string, not a generated ID)
//	Create:  POST   /api/terrapod/v1/users
//	Read:    GET    /api/terrapod/v1/users/{email}
//	Update:  PATCH  /api/terrapod/v1/users/{email}
//	Delete:  DELETE /api/terrapod/v1/users/{email}
//
// Migrated to go-terrapod (#347).
package user

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var (
	_ resource.Resource                = &userResource{}
	_ resource.ResourceWithImportState = &userResource{}
)

type userModel struct {
	ID types.String `tfsdk:"id"`

	Email       types.String `tfsdk:"email"`
	DisplayName types.String `tfsdk:"display_name"`
	IsActive    types.Bool   `tfsdk:"is_active"`
	Password    types.String `tfsdk:"password"`

	HasPassword types.Bool   `tfsdk:"has_password"`
	LastLoginAt types.String `tfsdk:"last_login_at"`
	CreatedAt   types.String `tfsdk:"created_at"`
	UpdatedAt   types.String `tfsdk:"updated_at"`
}

type userResource struct {
	client *client.Client
	tc     *terrapod.Client
}

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

	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *userResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan userModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	u, err := r.tc.CreateUser(ctx, buildCreateUserRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to create user", err.Error())
		return
	}

	readUserFromSDK(u, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *userResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	u, err := r.tc.GetUser(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Failed to read user", err.Error())
		return
	}

	// Preserve the write-only password (API never returns it).
	password := state.Password
	readUserFromSDK(u, &state)
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

	u, err := r.tc.UpdateUser(ctx, state.ID.ValueString(), buildUpdateUserRequest(&plan))
	if err != nil {
		resp.Diagnostics.AddError("Failed to update user", err.Error())
		return
	}

	password := plan.Password
	readUserFromSDK(u, &plan)
	plan.Password = password
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *userResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state userModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.tc.DeleteUser(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Failed to delete user", err.Error())
		}
	}
}

func (r *userResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), req.ID)...)
}

func buildCreateUserRequest(m *userModel) terrapod.CreateUserRequest {
	req := terrapod.CreateUserRequest{
		Email: m.Email.ValueString(),
	}
	if !m.DisplayName.IsNull() && !m.DisplayName.IsUnknown() {
		req.DisplayName = m.DisplayName.ValueString()
	}
	if !m.IsActive.IsNull() && !m.IsActive.IsUnknown() {
		v := m.IsActive.ValueBool()
		req.IsActive = &v
	}
	if !m.Password.IsNull() && !m.Password.IsUnknown() {
		req.Password = m.Password.ValueString()
	}
	return req
}

func buildUpdateUserRequest(m *userModel) terrapod.UpdateUserRequest {
	req := terrapod.UpdateUserRequest{}
	if !m.DisplayName.IsNull() && !m.DisplayName.IsUnknown() {
		v := m.DisplayName.ValueString()
		req.DisplayName = &v
	}
	if !m.IsActive.IsNull() && !m.IsActive.IsUnknown() {
		v := m.IsActive.ValueBool()
		req.IsActive = &v
	}
	if !m.Password.IsNull() && !m.Password.IsUnknown() {
		req.Password = m.Password.ValueString()
	}
	return req
}

func readUserFromSDK(u *terrapod.User, m *userModel) {
	m.ID = types.StringValue(u.Email)
	m.Email = types.StringValue(u.Email)

	if u.DisplayName != "" {
		m.DisplayName = types.StringValue(u.DisplayName)
	} else {
		m.DisplayName = types.StringNull()
	}

	m.IsActive = types.BoolValue(u.IsActive)
	m.HasPassword = types.BoolValue(u.HasPassword)

	if u.LastLoginAt != "" {
		m.LastLoginAt = types.StringValue(u.LastLoginAt)
	} else {
		m.LastLoginAt = types.StringNull()
	}

	m.CreatedAt = types.StringValue(u.CreatedAt)
	m.UpdatedAt = types.StringValue(u.UpdatedAt)
}
