package terrapod

# Production security groups must not accept traffic from the whole internet.
#
# The fixture's web tier deliberately violates this (0.0.0.0/0 on 443), so the
# policy screenshots show a REAL failure with a real resource address rather
# than a contrived example — and the same open rule is what the Checkov scan
# independently flags.

deny contains msg if {
	some change in input.resource_changes
	change.type == "aws_security_group"
	"create" in change.change.actions
	some rule in change.change.after.ingress
	"0.0.0.0/0" in rule.cidr_blocks
	msg := sprintf(
		"%s allows ingress from 0.0.0.0/0 on port %d — production ingress must come via the load balancer",
		[change.address, rule.from_port],
	)
}
