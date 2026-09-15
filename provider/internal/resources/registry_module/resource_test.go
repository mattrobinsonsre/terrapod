package registry_module

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func serverModule() *terrapod.RegistryModule {
	return &terrapod.RegistryModule{
		ID:            "mod-1",
		Name:          "network",
		ProviderName:  "aws",
		Namespace:     "default",
		Status:        "setup_complete",
		Source:        "vcs",
		VCSRepoURL:    "https://github.com/org/terraform-aws-network",
		VCSTagPattern: "v*",
		Subdirectory:  "modules/x",
	}
}

func emptyModel() *registryModuleModel {
	return &registryModuleModel{
		VCSConnectionID: types.StringNull(),
		VCSRepoURL:      types.StringNull(),
		VCSBranch:       types.StringNull(),
		VCSTagPattern:   types.StringNull(),
		Subdirectory:    types.StringNull(),
		Labels:          types.MapNull(types.StringType),
	}
}

// The server normalises "modules/x/" to "modules/x". Writing its value back
// over the planned one is what failed the apply with an inconsistent result.
func TestReadKeepsTheConfiguredSubdirectoryWhenTheServerNormalisesIt(t *testing.T) {
	for _, configured := range []string{"modules/x/", "/modules/x", " modules/x ", "modules/x"} {
		mod := emptyModel()
		mod.Subdirectory = types.StringValue(configured)
		if d := readModuleFromSDK(context.Background(), serverModule(), mod); d.HasError() {
			t.Fatal(d)
		}
		if got := mod.Subdirectory.ValueString(); got != configured {
			t.Errorf("configured %q: state should keep it, got %q", configured, got)
		}
	}
}

func TestReadTakesTheServerSubdirectoryWhenItIsADifferentPath(t *testing.T) {
	mod := emptyModel()
	mod.Subdirectory = types.StringValue("modules/y/")
	_ = readModuleFromSDK(context.Background(), serverModule(), mod)
	if got := mod.Subdirectory.ValueString(); got != "modules/x" {
		t.Errorf("a changed path must take the server's value, got %q", got)
	}
}

// An empty subdirectory stays "" — not null — so it matches the "" default the
// plan carries, rather than failing the apply the way "" coming back as null did.
func TestReadKeepsAnEmptySubdirectoryAsEmptyNotNull(t *testing.T) {
	m := serverModule()
	m.Subdirectory = ""

	configured := emptyModel()
	configured.Subdirectory = types.StringValue("")
	_ = readModuleFromSDK(context.Background(), m, configured)
	if configured.Subdirectory.IsNull() || configured.Subdirectory.ValueString() != "" {
		t.Errorf("configured \"\" must stay \"\", got %v", configured.Subdirectory)
	}

	imported := emptyModel() // after import nothing is known
	_ = readModuleFromSDK(context.Background(), m, imported)
	if imported.Subdirectory.IsNull() || imported.Subdirectory.ValueString() != "" {
		t.Errorf("an imported root module should read as \"\", got %v", imported.Subdirectory)
	}
}

func TestReadTagPatternDefaultsAndKeepsAnEmptyConfiguredForm(t *testing.T) {
	omitted := emptyModel()
	_ = readModuleFromSDK(context.Background(), serverModule(), omitted)
	if omitted.VCSTagPattern.ValueString() != "v*" {
		t.Errorf("an unset tag pattern reads as the server's v*, got %v", omitted.VCSTagPattern)
	}

	empty := emptyModel()
	empty.VCSTagPattern = types.StringValue("")
	_ = readModuleFromSDK(context.Background(), serverModule(), empty)
	if empty.VCSTagPattern.IsNull() || empty.VCSTagPattern.ValueString() != "" {
		t.Errorf("\"\" means v* to the server, so the configured \"\" should be kept, got %v", empty.VCSTagPattern)
	}

	other := emptyModel()
	other.VCSTagPattern = types.StringValue("release-*")
	_ = readModuleFromSDK(context.Background(), serverModule(), other)
	if other.VCSTagPattern.ValueString() != "v*" {
		t.Errorf("a pattern the server does not hold must take its value, got %v", other.VCSTagPattern)
	}
}

func TestReadKeepsEquivalentOptionalVCSForms(t *testing.T) {
	m := serverModule()
	m.VCSConnectionID = "vcs-1111"

	mod := emptyModel()
	mod.VCSConnectionID = types.StringValue("1111")
	mod.VCSBranch = types.StringValue("")
	_ = readModuleFromSDK(context.Background(), m, mod)
	if mod.VCSConnectionID.ValueString() != "1111" {
		t.Errorf("a bare connection id naming the same connection should be kept, got %v", mod.VCSConnectionID)
	}
	if mod.VCSBranch.IsNull() || mod.VCSBranch.ValueString() != "" {
		t.Errorf("a configured \"\" branch should stay \"\", got %v", mod.VCSBranch)
	}

	unset := emptyModel()
	_ = readModuleFromSDK(context.Background(), m, unset)
	if !unset.VCSBranch.IsNull() {
		t.Errorf("an unconfigured empty branch stays null, got %v", unset.VCSBranch)
	}
	if unset.VCSConnectionID.ValueString() != "vcs-1111" {
		t.Errorf("an unconfigured connection takes the server's id, got %v", unset.VCSConnectionID)
	}
}

// Removing subdirectory from configuration plans its "" default; the update has
// to send that "" or the server keeps the old path and the state diverges.
func TestUpdateRequestSendsAnEmptySubdirectoryToClearIt(t *testing.T) {
	plan := emptyModel()
	plan.Subdirectory = types.StringValue("")
	plan.VCSTagPattern = types.StringValue("v*")
	req := updateRequestFromPlan(plan)
	if req.Subdirectory == nil || *req.Subdirectory != "" {
		t.Errorf("an empty subdirectory must be sent as \"\", got %v", req.Subdirectory)
	}
	if req.VCSTagPattern == nil || *req.VCSTagPattern != "v*" {
		t.Errorf("tag pattern: %v", req.VCSTagPattern)
	}
	if req.VCSConnectionID != nil || req.VCSRepoURL != nil || req.VCSBranch != nil || req.Labels != nil {
		t.Errorf("unconfigured fields must not be sent: %+v", req)
	}
}

func TestSchemaSubdirectoryAndTagPatternAreComputedWithDefaults(t *testing.T) {
	resp := &resource.SchemaResponse{}
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, resp)
	for _, name := range []string{"subdirectory", "vcs_tag_pattern"} {
		a, ok := resp.Schema.Attributes[name].(schema.StringAttribute)
		if !ok {
			t.Fatalf("%s: not a string attribute", name)
		}
		if !a.Optional || !a.Computed || a.Default == nil {
			t.Errorf("%s: want Optional+Computed with a default, got optional=%t computed=%t default=%v",
				name, a.Optional, a.Computed, a.Default)
		}
	}
}
