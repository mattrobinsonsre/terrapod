// Package stateupgrade holds the state upgraders shared between resources.
//
// It exists because the same schema change landed on two resources at once:
// #1480 took `allow_labels` and `deny_labels` from map(string) to
// map(list(string)) on BOTH `terrapod_role` and `terrapod_policy_set`, and
// neither declared a schema version or an upgrader. State written by a 1.x
// provider therefore could not be read at all -- every plan failed with
// `Unable to Read Previously Saved State for UpgradeResourceState`, and it
// failed even after the configuration had been updated to the new type,
// because the configuration was never the problem (#2452).
//
// Two copies of this would drift. `mapFromTFMap` is already duplicated between
// those two resources, which is the shape to avoid repeating.
package stateupgrade

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// LabelsV0Type is what a label map held before #1480: one value per key, which
// could not express the OR the server has always supported.
var LabelsV0Type attr.Type = types.StringType

// LabelsV1Type is what it holds now: the accepted values for that key.
var LabelsV1Type attr.Type = types.ListType{ElemType: types.StringType}

// PriorSchemaWithScalarLabels returns `current` with `names` put back the way
// version 0 had them, and the version set to 0.
//
// DERIVED from the current schema rather than written out, which is exact only
// while the resource's attribute SET has not moved since the last 1.x release.
// That is measured for both callers -- role 16 either side, policy set 23 --
// and each has a test that fails if it stops being true, because an attribute
// added later would otherwise be asserted to have existed in v0 state that
// never carried it.
func PriorSchemaWithScalarLabels(current schema.Schema, names ...string) *schema.Schema {
	current.Version = 0
	attrs := make(map[string]schema.Attribute, len(current.Attributes))
	for name, a := range current.Attributes {
		attrs[name] = a
	}
	for _, name := range names {
		attrs[name] = schema.MapAttribute{
			Description: "Version 0: one value per key.",
			Optional:    true,
			ElementType: LabelsV0Type,
		}
	}
	current.Attributes = attrs
	return &current
}

// WrapLabelValues turns `{k: "v"}` into `{k: ["v"]}`, which is what the same
// rule means under the new type.
//
// A null or unknown map stays null AT THE NEW ELEMENT TYPE. Returning a null
// map still typed map(string) would carry v0's type into v1 state, and the
// mismatch surfaces later on a resource that simply had no labels -- the
// common case, so that mistake breaks more than the bug it fixes.
func WrapLabelValues(ctx context.Context, in types.Map, diags *diag.Diagnostics) types.Map {
	if in.IsNull() || in.IsUnknown() {
		return types.MapNull(LabelsV1Type)
	}

	var flat map[string]string
	diags.Append(in.ElementsAs(ctx, &flat, false)...)
	if diags.HasError() {
		return types.MapNull(LabelsV1Type)
	}

	wrapped := make(map[string]attr.Value, len(flat))
	for key, value := range flat {
		list, d := types.ListValueFrom(ctx, types.StringType, []string{value})
		diags.Append(d...)
		if d.HasError() {
			return types.MapNull(LabelsV1Type)
		}
		wrapped[key] = list
	}

	out, d := types.MapValue(LabelsV1Type, wrapped)
	diags.Append(d...)
	return out
}
