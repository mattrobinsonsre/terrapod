package terrapod

# Data stores must be encrypted at rest. The fixture satisfies this, so the
# policy view shows a pass alongside the failure — a screen of nothing but
# failures reads as a broken estate rather than as a working guardrail.

deny contains msg if {
	some change in input.resource_changes
	change.type == "aws_rds_cluster"
	"create" in change.change.actions
	not change.change.after.storage_encrypted
	msg := sprintf("%s must set storage_encrypted", [change.address])
}
