package catalog_instance

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestCatalogInstanceUpdateBlockRefresh(t *testing.T) {
	state := catalogInstanceModel{InputValues: types.MapNull(types.StringType)}
	inst := &terrapod.CatalogInstance{
		ID:          "ws-1",
		Attributes:  map[string]any{},
		UpdateBlock: map[string]string{"version": "2.0.0", "reason": "inputs"},
	}
	if diags := readCatalogInstanceIntoModel(context.Background(), inst, &state); diags.HasError() {
		t.Fatal(diags)
	}
	if state.UpdateBlockVersion.ValueString() != "2.0.0" || state.UpdateBlockReason.ValueString() != "inputs" {
		t.Fatalf("blocked update: version=%v reason=%v", state.UpdateBlockVersion, state.UpdateBlockReason)
	}

	inst.UpdateBlock = nil
	if diags := readCatalogInstanceIntoModel(context.Background(), inst, &state); diags.HasError() {
		t.Fatal(diags)
	}
	if !state.UpdateBlockVersion.IsNull() || !state.UpdateBlockReason.IsNull() {
		t.Fatalf("cleared update block: version=%v reason=%v", state.UpdateBlockVersion, state.UpdateBlockReason)
	}
}
