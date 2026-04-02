// Package gpg_key implements the terrapod_gpg_key resource.
//
// API Contract (Terrapod API ↔ Terraform Provider):
//
//	JSON:API type: "gpg-keys"
//	ID: UUID (no prefix)
//	Create:  POST   /api/registry/private/v2/gpg-keys
//	Read:    GET    /api/registry/private/v2/gpg-keys/{id}
//	Delete:  DELETE /api/registry/private/v2/gpg-keys/{id}
//	No update — immutable resource.
//
// Attribute mapping:
//
//	"ascii-armor" → ascii_armor (string, required, write-only, forces new)
//	"namespace"   → namespace   (string, optional, default "default")
//	"source"      → source      (string, optional, default "terrapod")
//	"source-url"  → source_url  (string, optional)
//
// Read-only:
//
//	"key-id"     → key_id     (string, computed — extracted from armor)
//	"created-at" → created_at (string, computed)
//	"updated-at" → updated_at (string, computed)
//
// Import: by GPG key ID.
package gpg_key

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

type gpgKeyModel struct {
	ID         types.String `tfsdk:"id"`
	ASCIIArmor types.String `tfsdk:"ascii_armor"`
	Namespace  types.String `tfsdk:"namespace"`
	Source     types.String `tfsdk:"source"`
	SourceURL  types.String `tfsdk:"source_url"`
	KeyID      types.String `tfsdk:"key_id"`
	CreatedAt  types.String `tfsdk:"created_at"`
	UpdatedAt  types.String `tfsdk:"updated_at"`
}

var (
	_ resource.Resource                = &gpgKeyResource{}
	_ resource.ResourceWithImportState = &gpgKeyResource{}
)

type gpgKeyResource struct {
	client *client.Client
}

func NewResource() resource.Resource {
	return &gpgKeyResource{}
}

func (r *gpgKeyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_gpg_key"
}

func (r *gpgKeyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a GPG key for provider signing in the Terrapod registry.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true, Description: "GPG key ID.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"ascii_armor": schema.StringAttribute{
				Required: true, Sensitive: true,
				Description: "ASCII-armored PGP public key (write-only).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"namespace": schema.StringAttribute{
				Optional: true, Computed: true,
				Default: stringdefault.StaticString("default"),
				Description: "Namespace.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"source": schema.StringAttribute{
				Optional: true, Computed: true,
				Default: stringdefault.StaticString("terrapod"),
				Description: "Key source.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"source_url": schema.StringAttribute{
				Optional: true, Description: "Source URL.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"key_id": schema.StringAttribute{
				Computed: true, Description: "PGP key ID (extracted from armor).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"created_at": schema.StringAttribute{
				Computed: true, Description: "Creation timestamp.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"updated_at": schema.StringAttribute{
				Computed: true, Description: "Update timestamp.",
			},
		},
	}
}

func (r *gpgKeyResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *gpgKeyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan gpgKeyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	attrs := map[string]any{
		"ascii-armor": plan.ASCIIArmor.ValueString(),
		"namespace":   plan.Namespace.ValueString(),
		"source":      plan.Source.ValueString(),
	}
	if !plan.SourceURL.IsNull() {
		attrs["source-url"] = plan.SourceURL.ValueString()
	}

	body, err := client.MarshalResource("gpg-keys", attrs, nil)
	if err != nil {
		resp.Diagnostics.AddError("Marshal error", err.Error())
		return
	}

	data, err := r.client.Post(ctx, "/api/registry/private/v2/gpg-keys", body)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}

	res, err := client.ParseResource(data)
	if err != nil {
		resp.Diagnostics.AddError("Parse error", err.Error())
		return
	}

	readGPGKeyIntoModel(res, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *gpgKeyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state gpgKeyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	data, err := r.client.Get(ctx, "/api/registry/private/v2/gpg-keys/"+state.ID.ValueString())
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

	readGPGKeyIntoModel(res, &state)
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *gpgKeyResource) Update(_ context.Context, _ resource.UpdateRequest, resp *resource.UpdateResponse) {
	resp.Diagnostics.AddError("Update not supported", "GPG keys are immutable. Delete and recreate instead.")
}

func (r *gpgKeyResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state gpgKeyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	err := r.client.Delete(ctx, "/api/registry/private/v2/gpg-keys/"+state.ID.ValueString())
	if err != nil && !client.IsNotFound(err) {
		resp.Diagnostics.AddError("Delete failed", err.Error())
	}
}

func (r *gpgKeyResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func readGPGKeyIntoModel(res *client.Resource, m *gpgKeyModel) {
	m.ID = types.StringValue(res.ID)
	m.KeyID = types.StringValue(client.GetStringAttr(res, "key-id"))
	m.Namespace = types.StringValue(client.GetStringAttr(res, "namespace"))
	m.Source = types.StringValue(client.GetStringAttr(res, "source"))
	m.CreatedAt = types.StringValue(client.GetStringAttr(res, "created-at"))
	m.UpdatedAt = types.StringValue(client.GetStringAttr(res, "updated-at"))

	if v := client.GetStringAttr(res, "source-url"); v != "" {
		m.SourceURL = types.StringValue(v)
	} else {
		m.SourceURL = types.StringNull()
	}
	// ascii_armor is write-only — preserved from plan/config
}
