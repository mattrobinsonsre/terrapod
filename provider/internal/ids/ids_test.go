package ids

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

func TestSameDiscountsThePrefixOnEitherSide(t *testing.T) {
	const u = "01a085db-3a9c-7fab-83a5-87b2d5e619d0"
	for _, c := range []struct {
		name, a, b string
		want       bool
	}{
		{"prefixed against bare", "apool-" + u, u, true},
		{"bare against prefixed", u, "apool-" + u, true},
		{"both prefixed", "apool-" + u, "apool-" + u, true},
		{"both bare", u, u, true},
		{"different pools", "apool-" + u, "apool-00000000-0000-0000-0000-000000000000", false},
		// A uuid has hyphens of its own, so only the named prefix is discounted
		// -- never "everything up to the first hyphen".
		{"not the uuid's own hyphens", "01a085db-x", "3a9c-x", false},
		{"both empty", "", "", true},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := Same(c.a, c.b, "apool-"); got != c.want {
				t.Fatalf("Same(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
			}
		})
	}
}

func TestKeepPrefersTheConfiguredForm(t *testing.T) {
	const u = "01a0a4aa-9d02-719d-b0be-ce434341268c"

	// The reported bug (#1748): the config interpolates a data source's `.id`,
	// which is prefixed, and this endpoint answers bare. Keeping the configured
	// form is what stops Terraform calling that an inconsistent result.
	got := Keep(types.StringValue("vcs-"+u), u, "vcs-")
	if got.ValueString() != "vcs-"+u {
		t.Fatalf("configured prefixed form not kept: got %q", got.ValueString())
	}

	// The other direction must hold too, or fixing one form breaks the people
	// who adopted the bare-id workaround.
	got = Keep(types.StringValue(u), u, "vcs-")
	if got.ValueString() != u {
		t.Fatalf("configured bare form not kept: got %q", got.ValueString())
	}
}

func TestKeepLetsTheServerWinOnRealDrift(t *testing.T) {
	// Someone repointed the rule outside Terraform. That is not a difference of
	// spelling, and hiding it would turn drift detection into a no-op.
	const mine = "01a0a4aa-9d02-719d-b0be-ce434341268c"
	const theirs = "01a0b524-c507-7d83-a87c-796de60b400d"

	got := Keep(types.StringValue("vcs-"+mine), theirs, "vcs-")
	if got.ValueString() != theirs {
		t.Fatalf("drift was swallowed: got %q, want the server's %q", got.ValueString(), theirs)
	}
}

func TestKeepWithNothingConfigured(t *testing.T) {
	// Null and unknown carry no form to preserve, so the server's value is
	// taken verbatim -- including the empty string, which is what this resource
	// has always stored for an absent optional id.
	if got := Keep(types.StringNull(), "apool-x", "apool-"); got.ValueString() != "apool-x" {
		t.Fatalf("null configured: got %q", got.ValueString())
	}
	if got := Keep(types.StringUnknown(), "apool-x", "apool-"); got.ValueString() != "apool-x" {
		t.Fatalf("unknown configured: got %q", got.ValueString())
	}
	if got := Keep(types.StringNull(), "", "apool-"); got.IsNull() || got.ValueString() != "" {
		t.Fatalf("absent optional id should stay the empty string, got %#v", got)
	}
}
