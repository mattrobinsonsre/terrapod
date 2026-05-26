// Package gpg_key implements the terrapod_gpg_key resource.
// Migrated to go-terrapod (#347).
package gpg_key

import (
	"context"
	"errors"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
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
	tc     *terrapod.Client
}

func NewResource() resource.Resource { return &gpgKeyResource{} }

func (r *gpgKeyResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_gpg_key"
}

func (r *gpgKeyResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a GPG key for provider signing in the Terrapod registry.",
		Attributes: map[string]schema.Attribute{
			"id":         schema.StringAttribute{Computed: true, Description: "GPG key ID.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"ascii_armor": schema.StringAttribute{Required: true, Sensitive: true, Description: "ASCII-armored PGP public key (write-only).", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"namespace":  schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("default"), Description: "Namespace.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"source":     schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString("terrapod"), Description: "Key source.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"source_url": schema.StringAttribute{Optional: true, Description: "Source URL.", PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()}},
			"key_id":     schema.StringAttribute{Computed: true, Description: "PGP key ID (extracted from armor).", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"created_at": schema.StringAttribute{Computed: true, Description: "Creation timestamp.", PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
			"updated_at": schema.StringAttribute{Computed: true, Description: "Update timestamp."},
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
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	r.tc = tc
}

func (r *gpgKeyResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan gpgKeyModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	sdkReq := terrapod.CreateGPGKeyRequest{
		ASCIIArmor: plan.ASCIIArmor.ValueString(),
		Namespace:  plan.Namespace.ValueString(),
		Source:     plan.Source.ValueString(),
	}
	if !plan.SourceURL.IsNull() {
		sdkReq.SourceURL = plan.SourceURL.ValueString()
	}
	k, err := r.tc.CreateGPGKey(ctx, sdkReq)
	if err != nil {
		resp.Diagnostics.AddError("Create failed", err.Error())
		return
	}
	readGPGKeyFromSDK(k, &plan)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *gpgKeyResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state gpgKeyModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	k, err := r.tc.GetGPGKey(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if errors.As(err, &nf) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read failed", err.Error())
		return
	}
	readGPGKeyFromSDK(k, &state)
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
	err := r.tc.DeleteGPGKey(ctx, state.ID.ValueString())
	if err != nil {
		var nf *terrapod.NotFoundError
		if !errors.As(err, &nf) {
			resp.Diagnostics.AddError("Delete failed", err.Error())
		}
	}
}

func (r *gpgKeyResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}

func readGPGKeyFromSDK(k *terrapod.GPGKey, m *gpgKeyModel) {
	m.ID = types.StringValue(k.ID)
	m.KeyID = types.StringValue(k.KeyID)
	m.Namespace = types.StringValue(k.Namespace)
	m.Source = types.StringValue(k.Source)
	m.CreatedAt = types.StringValue(k.CreatedAt)
	m.UpdatedAt = types.StringValue(k.UpdatedAt)
	if k.SourceURL != "" {
		m.SourceURL = types.StringValue(k.SourceURL)
	} else {
		m.SourceURL = types.StringNull()
	}
	// ascii_armor is write-only — preserved by Terraform from plan/config.
}
