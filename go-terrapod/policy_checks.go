package terrapod

import (
	"context"
	"fmt"
	"net/url"
	"strings"
)

// Run statuses Terrapod reports for a run a post-plan gate stopped, when the
// response uses the Terraform Enterprise vocabulary (#1704): the server is
// configured with `runs.tfe_post_plan_decisions`, or the request carries
// PostPlanDecisionsHeader. Otherwise such a run reports RunStatusPlanning and
// names the gate in Run.BlockedBy.
const (
	RunStatusPlanning                 = "planning"
	RunStatusPostPlanRunning          = "post_plan_running"
	RunStatusPostPlanAwaitingDecision = "post_plan_awaiting_decision"
	RunStatusPolicyOverride           = "policy_override"

	// PostPlanDecisionsHeader asks for a vocabulary per request: "tfe" or
	// "legacy". Without it the server's configured default applies.
	PostPlanDecisionsHeader = "X-Terrapod-Post-Plan-Decisions"
)

// Policy check statuses Terrapod reports.
const (
	PolicyCheckPassed     = "passed"
	PolicyCheckSoftFailed = "soft_failed"
	PolicyCheckOverridden = "overridden"
)

// PolicyCheck is one of a run's policy checks, in the Terraform Enterprise
// shape the tofu/terraform CLI reads (#1704). Terrapod serves one for its OPA
// policy sets (Scope "organization") and one for its security scan (Scope
// "workspace"), each only when that gate recorded something for the run.
type PolicyCheck struct {
	ID     string `json:"id"`
	RunID  string `json:"run-id"`
	Status string `json:"status"`
	Scope  string `json:"scope"`
	// IsOverridable is true while the check is soft_failed; CanOverride says
	// whether the caller may override it.
	IsOverridable  bool `json:"is-overridable"`
	CanOverride    bool `json:"can-override"`
	Passed         int  `json:"passed"`
	SoftFailed     int  `json:"soft-failed"`
	AdvisoryFailed int  `json:"advisory-failed"`
}

// ListRunPolicyChecks returns a run's policy checks: OPA first, then the scan.
func (c *Client) ListRunPolicyChecks(ctx context.Context, runID string) ([]PolicyCheck, error) {
	id, err := runIDPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/v2/runs/"+id+"/policy-checks")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse policy checks: %w", err)
	}
	checks := make([]PolicyCheck, 0, len(resources))
	for i := range resources {
		checks = append(checks, policyCheckFromResource(&resources[i]))
	}
	return checks, nil
}

// GetPolicyCheck reads one policy check by its "polchk-" id.
func (c *Client) GetPolicyCheck(ctx context.Context, checkID string) (*PolicyCheck, error) {
	id, err := policyCheckIDPath(checkID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, "/api/v2/policy-checks/"+id)
	if err != nil {
		return nil, err
	}
	return parsePolicyCheck(data)
}

// GetPolicyCheckOutput returns what the check found, as plain text.
func (c *Client) GetPolicyCheckOutput(ctx context.Context, checkID string) (string, error) {
	id, err := policyCheckIDPath(checkID)
	if err != nil {
		return "", err
	}
	data, err := c.Get(ctx, "/api/v2/policy-checks/"+id+"/output")
	if err != nil {
		return "", err
	}
	return string(data), nil
}

// OverridePolicyCheck overrides a soft-failed check, which requires admin on
// the workspace, and moves a run it was holding on at once. A check that is not
// soft_failed returns a *ConflictError.
func (c *Client) OverridePolicyCheck(ctx context.Context, checkID string) (*PolicyCheck, error) {
	id, err := policyCheckIDPath(checkID)
	if err != nil {
		return nil, err
	}
	data, err := c.Post(ctx, "/api/v2/policy-checks/"+id+"/actions/override", nil)
	if err != nil {
		return nil, err
	}
	return parsePolicyCheck(data)
}

func policyCheckIDPath(checkID string) (string, error) {
	if !strings.HasPrefix(checkID, "polchk-") {
		return "", fmt.Errorf("policy check id must start with polchk-, got %q", checkID)
	}
	return url.PathEscape(checkID), nil
}

func parsePolicyCheck(body []byte) (*PolicyCheck, error) {
	res, err := ParseResource(body)
	if err != nil {
		return nil, fmt.Errorf("parse policy check: %w", err)
	}
	pc := policyCheckFromResource(res)
	return &pc, nil
}

func policyCheckFromResource(res *Resource) PolicyCheck {
	pc := PolicyCheck{
		ID:     res.ID,
		RunID:  GetRelationshipID(res, "run"),
		Status: GetStringAttr(res, "status"),
		Scope:  GetStringAttr(res, "scope"),
	}
	if actions := GetObjectAttr(res, "actions"); actions != nil {
		pc.IsOverridable, _ = actions["is-overridable"].(bool)
	}
	if perms := GetObjectAttr(res, "permissions"); perms != nil {
		pc.CanOverride, _ = perms["can-override"].(bool)
	}
	if result := GetObjectAttr(res, "result"); result != nil {
		pc.Passed = intOf(result["passed"])
		pc.SoftFailed = intOf(result["soft-failed"])
		pc.AdvisoryFailed = intOf(result["advisory-failed"])
	}
	return pc
}

func intOf(v any) int {
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return 0
}
