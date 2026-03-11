package provider

import (
	"context"
	"os"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/mattrobinsonsre/terrapod/provider/internal/client"
	agentPoolDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/agent_pool"
	roleDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/role"
	userDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/user"
	vcsConnectionDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/vcs_connection"
	workspaceDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/workspace"
	workspacesDS "github.com/mattrobinsonsre/terrapod/provider/internal/datasources/workspaces"
	agentPoolRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/agent_pool"
	agentPoolTokenRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/agent_pool_token"
	gpgKeyRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/gpg_key"
	notificationConfigRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/notification_configuration"
	registryModuleRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/registry_module"
	registryProviderRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/registry_provider"
	roleRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/role"
	roleAssignmentRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/role_assignment"
	runTaskRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/run_task"
	runTriggerRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/run_trigger"
	userRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/user"
	variableRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/variable"
	variableSetRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/variable_set"
	variableSetVarRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/variable_set_variable"
	variableSetWsRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/variable_set_workspace"
	vcsConnectionRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/vcs_connection"
	workspaceRes "github.com/mattrobinsonsre/terrapod/provider/internal/resources/workspace"
)

var _ provider.Provider = &terrapodProvider{}

type terrapodProvider struct {
	version string
}

type terrapodProviderModel struct {
	Hostname      types.String `tfsdk:"hostname"`
	Token         types.String `tfsdk:"token"`
	SkipTLSVerify types.Bool   `tfsdk:"skip_tls_verify"`
}

// New returns a new provider.Provider.
func New(version string) func() provider.Provider {
	return func() provider.Provider {
		return &terrapodProvider{version: version}
	}
}

func (p *terrapodProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "terrapod"
	resp.Version = p.version
}

func (p *terrapodProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manage Terrapod resources (workspaces, variables, roles, etc.).",
		Attributes: map[string]schema.Attribute{
			"hostname": schema.StringAttribute{
				Description: "The hostname of the Terrapod instance (e.g. terrapod.example.com). Can also be set via TERRAPOD_HOSTNAME.",
				Optional:    true,
			},
			"token": schema.StringAttribute{
				Description: "API token for authentication. Can also be set via TERRAPOD_TOKEN.",
				Optional:    true,
				Sensitive:   true,
			},
			"skip_tls_verify": schema.BoolAttribute{
				Description: "Skip TLS certificate verification. Can also be set via TERRAPOD_SKIP_TLS_VERIFY.",
				Optional:    true,
			},
		},
	}
}

func (p *terrapodProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var config terrapodProviderModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	hostname := os.Getenv("TERRAPOD_HOSTNAME")
	if !config.Hostname.IsNull() {
		hostname = config.Hostname.ValueString()
	}
	if hostname == "" {
		resp.Diagnostics.AddError("Missing hostname", "Set the hostname attribute or TERRAPOD_HOSTNAME environment variable.")
		return
	}

	token := os.Getenv("TERRAPOD_TOKEN")
	if !config.Token.IsNull() {
		token = config.Token.ValueString()
	}
	if token == "" {
		resp.Diagnostics.AddError("Missing token", "Set the token attribute or TERRAPOD_TOKEN environment variable.")
		return
	}

	skipTLS := os.Getenv("TERRAPOD_SKIP_TLS_VERIFY") == "true" || os.Getenv("TERRAPOD_SKIP_TLS_VERIFY") == "1"
	if !config.SkipTLSVerify.IsNull() {
		skipTLS = config.SkipTLSVerify.ValueBool()
	}

	c := client.NewClient(hostname, token, skipTLS)

	resp.DataSourceData = c
	resp.ResourceData = c
}

func (p *terrapodProvider) Resources(_ context.Context) []func() resource.Resource {
	return []func() resource.Resource{
		workspaceRes.NewResource,
		variableRes.NewResource,
		variableSetRes.NewResource,
		variableSetVarRes.NewResource,
		variableSetWsRes.NewResource,
		runTriggerRes.NewResource,
		notificationConfigRes.NewResource,
		runTaskRes.NewResource,
		roleRes.NewResource,
		roleAssignmentRes.NewResource,
		userRes.NewResource,
		vcsConnectionRes.NewResource,
		agentPoolRes.NewResource,
		agentPoolTokenRes.NewResource,
		registryModuleRes.NewResource,
		registryProviderRes.NewResource,
		gpgKeyRes.NewResource,
	}
}

func (p *terrapodProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{
		workspaceDS.NewDataSource,
		workspacesDS.NewDataSource,
		roleDS.NewDataSource,
		agentPoolDS.NewDataSource,
		vcsConnectionDS.NewDataSource,
		userDS.NewDataSource,
	}
}
