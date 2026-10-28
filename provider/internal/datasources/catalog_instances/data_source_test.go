package catalog_instances

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
)

func TestCatalogInstancesExposeUpdateBlock(t *testing.T) {
	var resp datasource.SchemaResponse
	NewDataSource().Schema(t.Context(), datasource.SchemaRequest{}, &resp)
	entries := resp.Schema.Attributes["instances"].(schema.ListNestedAttribute).NestedObject.Attributes
	for _, name := range []string{"update_block_version", "update_block_reason"} {
		attr, ok := entries[name].(schema.StringAttribute)
		if !ok || !attr.Computed {
			t.Errorf("%s must be a computed instance attribute", name)
		}
	}
}
