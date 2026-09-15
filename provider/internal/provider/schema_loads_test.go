package provider

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/hashicorp/terraform-plugin-go/tfprotov6"
)

// TestTheProviderSchemaLoads asks for the provider's schema through the
// protocol server, as terraform and tofu do on every init, validate and plan.
// That is where the framework validates every resource and data source,
// reserved attribute names included, and a single error there makes the whole
// provider unusable, even for configurations that never touch the offending
// resource (#1652). Reading each schema directly, as the schema contract test
// does, skips that validation.
func TestTheProviderSchemaLoads(t *testing.T) {
	srv, err := providerserver.NewProtocol6WithError(New("test")())()
	if err != nil {
		t.Fatalf("provider server: %v", err)
	}
	resp, err := srv.GetProviderSchema(context.Background(), &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatalf("GetProviderSchema: %v", err)
	}
	for _, d := range resp.Diagnostics {
		if d.Severity == tfprotov6.DiagnosticSeverityError {
			t.Errorf("provider schema does not load: %s: %s", d.Summary, d.Detail)
		}
	}
	if len(resp.ResourceSchemas) == 0 || len(resp.DataSourceSchemas) == 0 {
		t.Fatalf("expected resource and data source schemas, got %d and %d",
			len(resp.ResourceSchemas), len(resp.DataSourceSchemas))
	}
}
