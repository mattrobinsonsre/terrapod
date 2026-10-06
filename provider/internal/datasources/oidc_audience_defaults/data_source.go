// Package oidc_audience_defaults implements the
// terrapod_oidc_audience_defaults data source (#1901): the deployment-wide
// cloud-identity audience catalogue that a workspace's own `oidc_audiences` map
// merges OVER, per key.
//
// It exists because the merge is otherwise unobservable. A workspace read
// returns the MERGED map with no marker saying which entries the workspace
// owns, so an operator cannot tell an inherited entry from one of their own --
// and `terrapod_workspace.oidc_audiences` deliberately reconciles only the keys
// the practitioner declared, which means the keys they did NOT declare are
// invisible from the resource alone.
//
// So this is the other half of the pair, the way `default_tags` is the other
// half of `tags`: the resource tells you what you set, this tells you what you
// would inherit if you set nothing. Composing a trust policy from the effective
// set reads the merged map off the `terrapod_workspace` DATA source, which has
// no round-trip requirement and can therefore carry it.
package oidc_audience_defaults

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
)

var _ datasource.DataSource = &oidcAudienceDefaultsDataSource{}

type oidcAudienceDefaultsDataSource struct {
	tc *terrapod.Client
}

type oidcAudienceDefaultsModel struct {
	Audiences     types.Map  `tfsdk:"audiences"`
	IssuerEnabled types.Bool `tfsdk:"issuer_enabled"`
}

// NewDataSource returns a new OIDC audience defaults data source.
func NewDataSource() datasource.DataSource {
	return &oidcAudienceDefaultsDataSource{}
}

func (d *oidcAudienceDefaultsDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_oidc_audience_defaults"
}

func (d *oidcAudienceDefaultsDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The deployment-wide cloud-identity audience catalogue a workspace's own " +
			"`oidc_audiences` map merges over, per key. Read it to see what a workspace would " +
			"INHERIT if it overrode nothing — the workspace resource carries only the keys you " +
			"declared, so the rest are otherwise invisible.",
		Attributes: map[string]schema.Attribute{
			"audiences": schema.MapAttribute{
				Computed:    true,
				ElementType: types.ListType{ElemType: types.StringType},
				Description: "Provider configuration → its audiences, keyed on the bare provider type " +
					"as a `provider` block writes it (`aws`, `vault`), optionally with an alias " +
					"(`aws.west`). Empty when the deployment configures no catalogue, which is the " +
					"default and is not an error. Lookup is specific-then-general, so a `vault.eu` " +
					"target is answered by a `vault` entry when there is no `vault.eu` one.",
			},
			"issuer_enabled": schema.BoolAttribute{
				Computed: true,
				Description: "Whether this deployment publishes an OIDC issuer at all " +
					"(`api.config.auth.oidc_issuer.enabled`). Reported separately because an empty " +
					"catalogue and a disabled issuer are different states with the same symptom — " +
					"\"my workspace minted nothing\" — and only one of them is fixed by adding audiences.",
			},
		},
	}
}

func (d *oidcAudienceDefaultsDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("Expected *client.Client, got %T", req.ProviderData))
		return
	}
	tc, err := terrapod.NewClient(terrapod.Options{BaseURL: c.BaseURL, Token: c.Token})
	if err != nil {
		resp.Diagnostics.AddError("Failed to build go-terrapod client", err.Error())
		return
	}
	d.tc = tc
}

func (d *oidcAudienceDefaultsDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config oidcAudienceDefaultsModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	defaults, err := d.tc.GetOIDCAudienceDefaults(ctx)
	if err != nil {
		resp.Diagnostics.AddError("Failed to read OIDC audience defaults", err.Error())
		return
	}

	// An empty catalogue is an empty map, never null. A practitioner indexing
	// into it for a key that is not there should get "no such key", not an
	// error about a null value — and the SDK already normalises nil to empty
	// for the same reason.
	audiences, diags := types.MapValueFrom(ctx, types.ListType{ElemType: types.StringType}, defaults.Audiences)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	config.Audiences = audiences
	config.IssuerEnabled = types.BoolValue(defaults.IssuerEnabled)

	resp.Diagnostics.Append(resp.State.Set(ctx, &config)...)
}
