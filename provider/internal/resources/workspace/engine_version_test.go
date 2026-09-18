package workspace

import (
	"context"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The engine version has two attribute names (#1559): `engine_version` is
// current, `terraform_version` is its deprecated alias. The alias keeps working
// indefinitely because the API keeps accepting the name go-tfe sends; only the
// provider attribute is deprecated.

func TestEngineVersionRequest_PrefersTheCurrentAttribute(t *testing.T) {
	cases := []struct {
		name      string
		engine    types.String
		terraform types.String
		want      string
	}{
		{
			name:      "only the alias, as a config written before the rename has it",
			engine:    types.StringNull(),
			terraform: types.StringValue("1.11.4"),
			want:      "1.11.4",
		},
		{
			name:      "only the current attribute",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringNull(),
			want:      "1.12.0",
		},
		{
			name:      "both agreeing — the alias is redundant, not conflicting",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringValue("1.12.0"),
			want:      "1.12.0",
		},
		{
			name:      "neither set leaves the server's default alone",
			engine:    types.StringNull(),
			terraform: types.StringNull(),
			want:      "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &workspaceModel{
				Name:          types.StringValue("w"),
				EngineVersion: tc.engine,
				// Unknown-vs-null does not matter to the guard; null is what a
				// config that omits an Optional+Computed attribute produces at
				// the point these builders run.
				TerraformVersion: tc.terraform,
			}
			create, diags := buildCreateWorkspaceRequest(context.Background(), m)
			if diags.HasError() {
				t.Fatalf("buildCreateWorkspaceRequest: %v", diags)
			}
			if create.EngineVersion != tc.want {
				t.Errorf("create EngineVersion = %q, want %q", create.EngineVersion, tc.want)
			}
			// The builders must never populate the SDK's alias field: it would
			// put a second key on the wire, and a pair that disagrees is a 422.
			if create.TerraformVersion != "" {
				t.Errorf("create set the SDK alias field: %q", create.TerraformVersion)
			}

			update, diags := buildUpdateWorkspaceRequest(context.Background(), m)
			if diags.HasError() {
				t.Fatalf("buildUpdateWorkspaceRequest: %v", diags)
			}
			if update.EngineVersion != tc.want {
				t.Errorf("update EngineVersion = %q, want %q", update.EngineVersion, tc.want)
			}
			if update.TerraformVersion != "" {
				t.Errorf("update set the SDK alias field: %q", update.TerraformVersion)
			}
		})
	}
}

func TestEngineVersionRead_FillsBothAttributes(t *testing.T) {
	// Both attributes take the server's one value. If only one were filled, a
	// config using the other would see it go null on every Read and plan the
	// same change forever.
	cases := []struct {
		name string
		ws   terrapod.Workspace
		want types.String
	}{
		{
			name: "server reports a version",
			ws:   terrapod.Workspace{EngineVersion: "1.12.0", TerraformVersion: "1.12.0"},
			want: types.StringValue("1.12.0"),
		},
		{
			name: "server reports none",
			ws:   terrapod.Workspace{},
			want: types.StringNull(),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := &workspaceModel{}
			if diags := readWorkspaceIntoModel(context.Background(), &tc.ws, m); diags.HasError() {
				t.Fatalf("readWorkspaceIntoModel: %v", diags)
			}
			if !m.EngineVersion.Equal(tc.want) {
				t.Errorf("EngineVersion = %v, want %v", m.EngineVersion, tc.want)
			}
			if !m.TerraformVersion.Equal(tc.want) {
				t.Errorf("TerraformVersion = %v, want %v", m.TerraformVersion, tc.want)
			}
		})
	}
}

func TestEngineVersionValidation_RejectsADisagreeingPair(t *testing.T) {
	// The negative path: one version under two names cannot be two versions.
	// The server refuses the pair at apply; this catches it at plan.
	cases := []struct {
		name      string
		engine    types.String
		terraform types.String
		wantError bool
	}{
		{
			name:      "different values is a config that contradicts itself",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringValue("1.11.4"),
			wantError: true,
		},
		{
			name:      "same value is redundant but legal",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringValue("1.12.0"),
		},
		{
			name:      "only one set cannot disagree",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringNull(),
		},
		{
			name:      "an unknown value cannot be compared yet",
			engine:    types.StringValue("1.12.0"),
			terraform: types.StringUnknown(),
		},
		{
			name:      "neither set",
			engine:    types.StringNull(),
			terraform: types.StringNull(),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var resp resource.ValidateConfigResponse
			validateEngineVersionPair(tc.engine, tc.terraform, &resp)
			if got := resp.Diagnostics.HasError(); got != tc.wantError {
				t.Fatalf("HasError() = %t, want %t (%v)", got, tc.wantError, resp.Diagnostics)
			}
			if !tc.wantError {
				return
			}
			// The message has to name both attributes and say which to keep,
			// or the operator is left to guess.
			summary := resp.Diagnostics.Errors()[0].Detail()
			for _, want := range []string{"engine_version", "terraform_version"} {
				if !strings.Contains(summary, want) {
					t.Errorf("error detail does not mention %s: %s", want, summary)
				}
			}
		})
	}
}
