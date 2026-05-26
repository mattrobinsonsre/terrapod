// Package notification_configuration — migrated to go-terrapod (#347).
package notification_configuration

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/diag"
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
	_ resource.Resource                = &notificationConfigResource{}
	_ resource.ResourceWithImportState = &notificationConfigResource{}
)

type notificationConfigResource struct {
	client *client.Client
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &notificationConfigResource{} }

func (r *notificationConfigResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_notification_configuration"
}

func (r *notificationConfigResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a notification configuration for a Terrapod workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, Description: "Notification configuration ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"workspace_id": schema.StringAttribute{Required: true, Description: "Workspace ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"name":            schema.StringAttribute{Required: true, Description: "Notification name."},
			"destination_type": schema.StringAttribute{Required: true, Description: "Type: generic, slack, or email.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"url":             schema.StringAttribute{Optional: true, Description: "Webhook URL (required for generic/slack)."},
			"token":           schema.StringAttribute{Optional: true, Sensitive: true, Description: "HMAC or auth token (write-only)."},
			"enabled":         schema.BoolAttribute{Optional: true, Computed: true, Default: booldefault.StaticBool(false), Description: "Whether the notification is enabled."},
			"triggers":        schema.ListAttribute{Optional: true, ElementType: types.StringType, Description: "Run event triggers."},
			"email_addresses": schema.ListAttribute{Optional: true, ElementType: types.StringType, Description: "Email addresses (for email destination type)."},
			"has_token":       schema.BoolAttribute{Computed: true, Description: "Whether a token is configured."},
			"created_at":      schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at":      schema.StringAttribute{Computed: true, Description: "Update timestamp."},
		},
	}
}

func (r *notificationConfigResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *notificationConfigResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan notificationConfigModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	nc, err := r.tc.CreateNotificationConfiguration(ctx, plan.WorkspaceID.ValueString(), buildCreateNCRequest(ctx, &plan))
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readNCFromSDK(ctx, nc, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationConfigResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state notificationConfigModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	nc, err := r.tc.GetNotificationConfiguration(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readNCFromSDK(ctx, nc, &state)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *notificationConfigResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan notificationConfigModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var state notificationConfigModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	nc, err := r.tc.UpdateNotificationConfiguration(ctx, state.ID.ValueString(), buildUpdateNCRequest(ctx, &plan))
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}
	resp.Diagnostics.Append(readNCFromSDK(ctx, nc, &plan)...)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationConfigResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state notificationConfigModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	err := r.tc.DeleteNotificationConfiguration(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *notificationConfigResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildCreateNCRequest(ctx context.Context, m *notificationConfigModel) terrapod.CreateNotificationConfigurationRequest {
	req := terrapod.CreateNotificationConfigurationRequest{
		Name:            m.Name.ValueString(),
		DestinationType: m.DestinationType.ValueString(),
	}
	if !m.URL.IsNull() {
		req.URL = m.URL.ValueString()
	}
	if !m.Token.IsNull() {
		req.Token = m.Token.ValueString()
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		req.Enabled = m.Enabled.ValueBool()
	}
	if !m.Triggers.IsNull() {
		var triggers []string
		m.Triggers.ElementsAs(ctx, &triggers, false)
		req.Triggers = triggers
	}
	if !m.EmailAddresses.IsNull() {
		var emails []string
		m.EmailAddresses.ElementsAs(ctx, &emails, false)
		req.EmailAddresses = emails
	}
	return req
}

func buildUpdateNCRequest(ctx context.Context, m *notificationConfigModel) terrapod.UpdateNotificationConfigurationRequest {
	req := terrapod.UpdateNotificationConfigurationRequest{
		Name: m.Name.ValueString(),
	}
	if !m.URL.IsNull() && !m.URL.IsUnknown() {
		u := m.URL.ValueString()
		req.URL = &u
	}
	if !m.Token.IsNull() && !m.Token.IsUnknown() {
		req.Token = m.Token.ValueString()
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		v := m.Enabled.ValueBool()
		req.Enabled = &v
	}
	if !m.Triggers.IsNull() && !m.Triggers.IsUnknown() {
		var triggers []string
		m.Triggers.ElementsAs(ctx, &triggers, false)
		req.Triggers = &triggers
	}
	if !m.EmailAddresses.IsNull() && !m.EmailAddresses.IsUnknown() {
		var emails []string
		m.EmailAddresses.ElementsAs(ctx, &emails, false)
		req.EmailAddresses = &emails
	}
	return req
}

func readNCFromSDK(ctx context.Context, nc *terrapod.NotificationConfiguration, m *notificationConfigModel) diag.Diagnostics {
	var diags diag.Diagnostics

	m.ID = types.StringValue(nc.ID)
	m.Name = types.StringValue(nc.Name)
	m.DestinationType = types.StringValue(nc.DestinationType)
	m.Enabled = types.BoolValue(nc.Enabled)
	m.HasToken = types.BoolValue(nc.HasToken)
	m.CreatedAt = types.StringValue(nc.CreatedAt)
	m.UpdatedAt = types.StringValue(nc.UpdatedAt)

	if nc.URL != "" {
		m.URL = types.StringValue(nc.URL)
	} else {
		m.URL = types.StringNull()
	}

	if len(nc.Triggers) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, nc.Triggers)
		diags.Append(d...)
		m.Triggers = val
	} else {
		m.Triggers = types.ListNull(types.StringType)
	}
	if len(nc.EmailAddresses) > 0 {
		val, d := types.ListValueFrom(ctx, types.StringType, nc.EmailAddresses)
		diags.Append(d...)
		m.EmailAddresses = val
	} else {
		m.EmailAddresses = types.ListNull(types.StringType)
	}

	return diags
}
