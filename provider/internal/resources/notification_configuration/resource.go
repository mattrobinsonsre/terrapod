package notification_configuration

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
	_ resource.Resource                = &notificationConfigResource{}
	_ resource.ResourceWithImportState = &notificationConfigResource{}
)

type notificationConfigResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &notificationConfigResource{}
}

func (r *notificationConfigResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_notification_configuration"
}

func (r *notificationConfigResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a notification configuration for a Terrapod workspace.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "Notification configuration ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"workspace_id": schema.StringAttribute{
				Required: true, Description: "Workspace ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"name": schema.StringAttribute{
				Required: true, Description: "Notification name.",
			},
			"destination_type": schema.StringAttribute{
				Required: true, Description: "Type: generic, slack, or email.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"url": schema.StringAttribute{
				Optional: true, Description: "Webhook URL (required for generic/slack).",
			},
			"token": schema.StringAttribute{
				Optional: true, Sensitive: true,
				Description: "HMAC or auth token (write-only).",
			},
			"enabled": schema.BoolAttribute{
				Optional: true, Computed: true, Default: booldefault.StaticBool(false),
				Description: "Whether the notification is enabled.",
			},
			"triggers": schema.ListAttribute{
				Optional: true, ElementType: types.StringType,
				Description: "Run event triggers.",
			},
			"email_addresses": schema.ListAttribute{
				Optional: true, ElementType: types.StringType,
				Description: "Email addresses (for email destination type).",
			},
			"has_token": schema.BoolAttribute{Computed: true, Description: "Whether a token is configured."},
			"created_at": schema.StringAttribute{Computed: true, Description: "Creation timestamp."},
			"updated_at": schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
}

func (r *notificationConfigResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan notificationConfigModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := buildAttrs(ctx, &plan)
	body, err := client.MarshalResource("notification-configurations", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, fmt.Sprintf("/api/v2/workspaces/%s/notification-configurations", plan.WorkspaceID.ValueString()), body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(ctx, res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationConfigResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state notificationConfigModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/v2/notification-configurations/"+state.ID.ValueString())
	if err != nil {
		if client.IsNotFound(err) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(ctx, res, &state)
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

	attrs := buildAttrs(ctx, &plan)
	body, err := client.MarshalResourceWithID(state.ID.ValueString(), "notification-configurations", attrs)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Patch(ctx, "/api/v2/notification-configurations/"+state.ID.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("Update failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readIntoModel(ctx, res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *notificationConfigResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state notificationConfigModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/v2/notification-configurations/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *notificationConfigResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func buildAttrs(ctx context.Context, m *notificationConfigModel) map[string]any {
	attrs := map[string]any{
		"name":             m.Name.ValueString(),
		"destination-type": m.DestinationType.ValueString(),
	}
	if !m.URL.IsNull() {
		attrs["url"] = m.URL.ValueString()
	}
	if !m.Token.IsNull() {
		attrs["token"] = m.Token.ValueString()
	}
	if !m.Enabled.IsNull() && !m.Enabled.IsUnknown() {
		attrs["enabled"] = m.Enabled.ValueBool()
	}
	if !m.Triggers.IsNull() {
		var triggers []string
		m.Triggers.ElementsAs(ctx, &triggers, false)
		attrs["triggers"] = triggers
	}
	if !m.EmailAddresses.IsNull() {
		var emails []string
		m.EmailAddresses.ElementsAs(ctx, &emails, false)
		attrs["email-addresses"] = emails
	}
	return attrs
}

func readIntoModel(ctx context.Context, res *client.Resource, m *notificationConfigModel) {
	m.ID = types.StringValue(res.ID)
	m.Name = types.StringValue(client.GetStringAttr(res, "name"))
	m.DestinationType = types.StringValue(client.GetStringAttr(res, "destination-type"))
	m.Enabled = types.BoolValue(client.GetBoolAttr(res, "enabled"))
	m.HasToken = types.BoolValue(client.GetBoolAttr(res, "has-token"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if v := client.GetStringAttr(res, "url"); v != "" {
		m.URL = types.StringValue(v)
	} else {
		m.URL = types.StringNull()
	}

	// Token is write-only — preserve from config
	triggers := client.GetListAttr(res, "triggers")
	if len(triggers) > 0 {
		m.Triggers, _ = types.ListValueFrom(ctx, types.StringType, triggers)
	} else {
		m.Triggers = types.ListNull(types.StringType)
	}

	emails := client.GetListAttr(res, "email-addresses")
	if len(emails) > 0 {
		m.EmailAddresses, _ = types.ListValueFrom(ctx, types.StringType, emails)
	} else {
		m.EmailAddresses = types.ListNull(types.StringType)
	}
}
