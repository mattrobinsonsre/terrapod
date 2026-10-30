package workspace

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// The four workspace attributes an explicit `= ""` could not round-trip (#2310),
// and the reason they needed a test of their own: the defect lived in the
// DISAGREEMENT between two legs, so a test of either leg alone passes on a
// half-fixed tree.
//
// An Optional+Computed attribute plans an explicit "" as "", not as unknown, so
// the framework then requires "" back from the apply. Both legs could drop it:
//
//   - the WRITE leg. `UpdateWorkspaceRequest`'s four fields were plain `string`
//     with `,omitempty`, which discards an empty value before serialisation, and
//     `buildUpdateWorkspaceRequest` assigned the value straight across. They are
//     `*string` now and the write leg takes an address, so the SDK's own
//     `req.X != nil` test sends the empty attribute rather than omitting it —
//     that pointer being non-nil is precisely what puts "" on the wire.
//   - the READ leg. The three VCS attributes mapped "" to null, which is the
//     "Provider produced inconsistent result after apply" shape against a plan
//     of "". They project the server's value unconditionally now.
//
// Nothing tested any of it: the whole provider suite passed before the fix as
// well as after.
//
// `ansible_execution_environment` is deliberately NOT like the other three and
// is tested separately below. Its schema records a decision against Computed,
// so it cannot take the unconditional read-back the VCS triplet gets.

// theFourEmptyStringAttributes is the write-leg subject: the model field each
// attribute reads from, and the request field it must reach. Driving them from
// one table is what keeps a later addition from being wired to only one of the
// two legs.
var theFourEmptyStringAttributes = []struct {
	name string
	set  func(*workspaceModel, types.String)
	get  func(terrapod.UpdateWorkspaceRequest) *string
}{
	{
		name: "ansible_execution_environment",
		set:  func(m *workspaceModel, v types.String) { m.AnsibleExecutionEnvironment = v },
		get:  func(r terrapod.UpdateWorkspaceRequest) *string { return r.AnsibleExecutionEnvironment },
	},
	{
		name: "vcs_repo_url",
		set:  func(m *workspaceModel, v types.String) { m.VCSRepoURL = v },
		get:  func(r terrapod.UpdateWorkspaceRequest) *string { return r.VCSRepoURL },
	},
	{
		name: "vcs_branch",
		set:  func(m *workspaceModel, v types.String) { m.VCSBranch = v },
		get:  func(r terrapod.UpdateWorkspaceRequest) *string { return r.VCSBranch },
	},
	{
		name: "vcs_connection_id",
		set:  func(m *workspaceModel, v types.String) { m.VCSConnectionID = v },
		get:  func(r terrapod.UpdateWorkspaceRequest) *string { return r.VCSConnectionID },
	},
}

// TestAnExplicitEmptyStringReachesTheUpdateRequest is the write leg: an
// operator who writes `= ""` is asking for the value to be cleared, and that
// request has to survive as far as the SDK.
//
// The pointer must be non-nil AND point at "". Non-nil is the whole mechanism —
// the SDK serialises on `req.X != nil`, so a nil pointer is an omitted
// attribute and the clear silently does nothing, which is how the apply came to
// fail with "Provider produced inconsistent result after apply".
func TestAnExplicitEmptyStringReachesTheUpdateRequest(t *testing.T) {
	ctx := context.Background()

	for _, a := range theFourEmptyStringAttributes {
		t.Run(a.name, func(t *testing.T) {
			var m workspaceModel
			a.set(&m, types.StringValue(""))

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("buildUpdateWorkspaceRequest: %v", diags)
			}

			got := a.get(update)
			if got == nil {
				t.Fatalf("update dropped an explicit empty %s — the one config that "+
					"clears it would silently do nothing, and the apply would then "+
					"fail against a planned \"\" (#2310)", a.name)
			}
			if *got != "" {
				t.Errorf("update sent %s=%q, want the empty string the config asked for",
					a.name, *got)
			}
		})
	}
}

// TestAnOmittedAttributeIsNotClearedByTheUpdateRequest is the other direction of
// the same rule, and it is what stops the careless fix.
//
// Assigning unconditionally would make every one of these pass the test above
// while turning a PATCH that mentions none of them into a PATCH that clears all
// four — so a practitioner who changed an unrelated attribute would wipe their
// workspace's VCS binding. A partial update must stay partial: null means "not
// mentioned", which is a different request from "set to empty".
func TestAnOmittedAttributeIsNotClearedByTheUpdateRequest(t *testing.T) {
	ctx := context.Background()

	for _, a := range theFourEmptyStringAttributes {
		t.Run(a.name, func(t *testing.T) {
			var m workspaceModel
			a.set(&m, types.StringNull())

			update, diags := buildUpdateWorkspaceRequest(ctx, &m)
			if diags.HasError() {
				t.Fatalf("buildUpdateWorkspaceRequest: %v", diags)
			}

			if got := a.get(update); got != nil {
				t.Errorf("update sent %s=%q for a null config value, want it omitted — "+
					"an attribute the PATCH does not mention must not be cleared",
					a.name, *got)
			}
		})
	}
}

// theVCSTriplet is the read-leg subject. `ansible_execution_environment` is
// absent on purpose: it is Optional-only and reads back through
// `optionalStringKeepingExplicitEmpty`, so an unconditional projection would be
// wrong for it. See TestTheExecutionEnvironmentLabelReadsBackFourWays.
var theVCSTriplet = []struct {
	name     string
	onServer func(*terrapod.Workspace, string)
	inModel  func(*workspaceModel) types.String
	setModel func(*workspaceModel, types.String)
}{
	{
		name:     "vcs_repo_url",
		onServer: func(ws *terrapod.Workspace, v string) { ws.VCSRepoURL = v },
		inModel:  func(m *workspaceModel) types.String { return m.VCSRepoURL },
		setModel: func(m *workspaceModel, v types.String) { m.VCSRepoURL = v },
	},
	{
		name:     "vcs_branch",
		onServer: func(ws *terrapod.Workspace, v string) { ws.VCSBranch = v },
		inModel:  func(m *workspaceModel) types.String { return m.VCSBranch },
		setModel: func(m *workspaceModel, v types.String) { m.VCSBranch = v },
	},
	{
		name:     "vcs_connection_id",
		onServer: func(ws *terrapod.Workspace, v string) { ws.VCSConnectionID = v },
		inModel:  func(m *workspaceModel) types.String { return m.VCSConnectionID },
		setModel: func(m *workspaceModel, v types.String) { m.VCSConnectionID = v },
	},
}

// TestTheVCSTripletReadsBackAnEmptyServerValueConcretely is the read leg.
//
// All three are Optional+Computed, which is what makes the unconditional
// projection the right answer: a config that omits the attribute plans UNKNOWN
// rather than null, so the server's "" fills it in and no spurious diff follows.
// Mapping "" to null instead — the `if x != "" { … } else { StringNull }` shape
// used elsewhere in the read-back for genuinely nullable attributes — would
// answer null against a planned "" and abort the apply.
//
// Both configured values are asserted because the projection must not depend on
// either: the one that would have caught #2310 is the explicit "", and the null
// row is the one that proves the attribute is genuinely Computed rather than
// quietly relying on the config to supply its own answer back.
func TestTheVCSTripletReadsBackAnEmptyServerValueConcretely(t *testing.T) {
	ctx := context.Background()

	configured := []struct {
		what string
		val  types.String
	}{
		{"an explicit empty string", types.StringValue("")},
		{"no value at all", types.StringNull()},
	}

	for _, a := range theVCSTriplet {
		for _, c := range configured {
			t.Run(a.name+"/"+c.what, func(t *testing.T) {
				ws := &terrapod.Workspace{ID: "ws-a", Name: "a"}
				a.onServer(ws, "")

				var m workspaceModel
				a.setModel(&m, c.val)

				if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
					t.Fatalf("readWorkspaceIntoModel: %v", diags)
				}

				got := a.inModel(&m)
				if got.IsNull() {
					t.Fatalf("%s read back null from a server value of \"\" with %s "+
						"configured — against a planned \"\" that is the "+
						"\"inconsistent result after apply\" shape (#2310)", a.name, c.what)
				}
				if got.IsUnknown() {
					t.Fatalf("%s read back unknown — the read-back must always answer "+
						"concretely for a Computed attribute", a.name)
				}
				if got.ValueString() != "" {
					t.Errorf("%s read back %q, want the server's empty value",
						a.name, got.ValueString())
				}
			})
		}
	}
}

// TestTheExecutionEnvironmentLabelReadsBackFourWays covers every combination,
// because this attribute's shape is only correct if it gets all four right and
// no smaller set of cases distinguishes it from the two wrong answers either
// side of it.
//
// It is Optional-only — the schema records the decision, and the test below
// guards it — so the server's "" for an unset label cannot simply be projected:
// that would write "" into state against a null config and diff for ever, which
// is what Computed exists to avoid and what this attribute cannot have.
// Equally it cannot map "" to null unconditionally, because then an explicit
// `= ""` never round-trips. `optionalStringKeepingExplicitEmpty` resolves it by
// consulting what the configuration asked for as well as what the server
// answered: null only when BOTH are empty.
//
// So the collapse-to-null rows and the explicit-"" row fail under opposite
// defects, and a test carrying only one of them would pass the other.
func TestTheExecutionEnvironmentLabelReadsBackFourWays(t *testing.T) {
	ctx := context.Background()

	cases := []struct {
		what       string
		configured types.String
		server     string
		wantNull   bool
		want       string
	}{
		{
			// The recorded decision being honoured: an omitted attribute must
			// not acquire a value the practitioner never wrote.
			what:       "omitted, and the server has no label",
			configured: types.StringNull(),
			server:     "",
			wantNull:   true,
		},
		{
			// Unknown is the same question asked during a create or a replace,
			// where the planned value is not yet resolved. It must answer null
			// for the same reason, not "".
			what:       "unknown, and the server has no label",
			configured: types.StringUnknown(),
			server:     "",
			wantNull:   true,
		},
		{
			// #2310 itself: the config asked for "", so "" is the correct
			// state, and null here fails the apply.
			what:       "an explicit empty string, which the server echoes",
			configured: types.StringValue(""),
			server:     "",
			want:       "",
		},
		{
			// The ordinary case, which must not regress while the empty-string
			// rows are being satisfied.
			what:       "a real label",
			configured: types.StringValue("x"),
			server:     "x",
			want:       "x",
		},
	}

	for _, c := range cases {
		t.Run(c.what, func(t *testing.T) {
			ws := &terrapod.Workspace{
				ID:                          "ws-a",
				Name:                        "a",
				AnsibleExecutionEnvironment: c.server,
			}

			var m workspaceModel
			m.AnsibleExecutionEnvironment = c.configured

			if diags := readWorkspaceIntoModel(ctx, ws, &m); diags.HasError() {
				t.Fatalf("readWorkspaceIntoModel: %v", diags)
			}

			got := m.AnsibleExecutionEnvironment
			if c.wantNull {
				if !got.IsNull() {
					t.Errorf("ansible_execution_environment read back %q with %s, want null "+
						"— the schema records a decision against Computed, so an "+
						"attribute the config never set must stay null rather than "+
						"acquiring the server's empty answer",
						got.ValueString(), c.what)
				}
				return
			}
			if got.IsNull() {
				t.Fatalf("ansible_execution_environment read back null with %s, want %q",
					c.what, c.want)
			}
			if got.IsUnknown() {
				t.Fatalf("ansible_execution_environment read back unknown with %s", c.what)
			}
			if got.ValueString() != c.want {
				t.Errorf("ansible_execution_environment read back %q with %s, want %q",
					got.ValueString(), c.what, c.want)
			}
		})
	}
}

// TestTheExecutionEnvironmentLabelStaysOptionalOnly exists to stop someone
// "tidying" this attribute onto the Optional+Computed shape its neighbours use,
// which would silently reverse the decision its schema records.
//
// The tidy-up is tempting precisely because every other string attribute around
// it is Optional+Computed, and it would look like a consistency fix. It is not:
// the server returns "" when no label is set and has no value of its own to
// supply, so Computed would put "" into state against a null config and produce
// a diff on every plan for every workspace that does not name a label. The
// read-back above is written the way it is BECAUSE this attribute is
// Optional-only — change this and `optionalStringKeepingExplicitEmpty` becomes
// the wrong helper for it.
func TestTheExecutionEnvironmentLabelStaysOptionalOnly(t *testing.T) {
	var resp resource.SchemaResponse
	NewResource().Schema(context.Background(), resource.SchemaRequest{}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatalf("schema build error: %v", resp.Diagnostics)
	}

	attr, ok := resp.Schema.Attributes["ansible_execution_environment"]
	if !ok {
		t.Fatal("ansible_execution_environment missing from the workspace schema")
	}
	if !attr.IsOptional() {
		t.Error("ansible_execution_environment must stay Optional — it is the " +
			"practitioner's own choice of execution environment")
	}
	if attr.IsComputed() {
		t.Error("ansible_execution_environment must NOT be Computed: the server " +
			"returns \"\" when no label is set and has no value to supply, so " +
			"Computed would write \"\" into state against a null config and diff " +
			"for ever. Its read-back relies on this — see " +
			"optionalStringKeepingExplicitEmpty and the schema's own note.")
	}
}
