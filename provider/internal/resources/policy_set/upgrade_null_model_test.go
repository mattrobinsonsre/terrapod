package policy_set

import (
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
)

// nullModel is a policySetModel with everything null but the label maps, so a
// v0 fixture does not have to enumerate attributes this upgrade never touches.
//
// The three list attributes are TYPED nulls rather than zero values: a
// zero-value types.List has no element type, and the framework rejects it as
// `types.ListType[!!! MISSING TYPE !!!]` when the state is set -- which reads
// as a defect in the upgrade rather than in the fixture.
func nullModel(t *testing.T, labels types.Map) *policySetModel {
	t.Helper()
	nullStrings := types.ListNull(types.StringType)
	return &policySetModel{
		ID:               types.StringValue("ps-1"),
		Name:             types.StringValue("baseline"),
		Description:      types.StringValue("the second instance"),
		EnforcementLevel: types.StringValue("mandatory"),
		Enabled:          types.BoolValue(true),
		SharedEvaluation: types.BoolValue(false),
		SupportFileNames: nullStrings,
		GlobalScope:      types.BoolValue(false),
		AllowLabels:      labels,
		AllowNames:       nullStrings,
		DenyLabels:       labels,
		DenyNames:        nullStrings,
		Source:           types.StringValue("inline"),
		VCSConnectionID:  types.StringNull(),
		VCSRepoURL:       types.StringNull(),
		VCSBranch:        types.StringNull(),
		PolicyPath:       types.StringNull(),
		VCSLastCommitSHA: types.StringNull(),
		VCSLastSyncedAt:  types.StringNull(),
		VCSLastError:     types.StringNull(),
		PolicyCount:      types.Int64Value(0),
		CreatedAt:        types.StringValue("2026-01-01T00:00:00Z"),
		UpdatedAt:        types.StringValue("2026-01-01T00:00:00Z"),
	}
}
