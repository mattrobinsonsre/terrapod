package mcpserver

import "testing"

// A Pulumi preview writes a digest into the same artifact slot a Terraform plan
// uses, and the two share no field. Unmarshalled into tfPlan the digest parses
// cleanly and yields nothing — so the compact view answered "0 to add, 0 to
// change, 0 to destroy" for a preview creating any number of resources, with no
// error and no hint that it was reading the wrong shape.
//
// A wrong answer given confidently is worse than a refusal, which is what these
// pin.

func TestAPulumiDigestIsRecognised(t *testing.T) {
	digest := []byte(`{"engine":"pulumi","change_summary":{"create":7},` +
		`"has_changes":true,"steps":[{"op":"create","urn":"urn:pulumi:dev::p::aws:s3/bucket:Bucket::b"}]}`)
	if got := planDocumentEngine(digest); got != "pulumi" {
		t.Fatalf("planDocumentEngine = %q, want %q — the digest would be read as a Terraform plan", got, "pulumi")
	}
}

func TestATerraformPlanNamesNoEngine(t *testing.T) {
	// `tofu show -json` carries no `engine` field, so the probe has to answer
	// empty rather than guess — otherwise every Terraform run takes the refusal.
	plan := []byte(`{"format_version":"1.2","terraform_version":"1.11.0","resource_changes":[]}`)
	if got := planDocumentEngine(plan); got != "" {
		t.Fatalf("planDocumentEngine = %q, want empty for a Terraform plan", got)
	}
}

func TestTheDigestWouldOtherwiseSummariseAsNoChanges(t *testing.T) {
	// The bug itself, pinned: without the probe this is what the agent is told
	// about a preview that creates seven resources.
	digest := []byte(`{"engine":"pulumi","change_summary":{"create":7},"has_changes":true}`)
	p, err := parsePlan(digest)
	if err != nil {
		t.Fatalf("the digest does not even fail to parse: %v", err)
	}
	s := summarise(p)
	if s.Add != 0 || s.Change != 0 || s.Destroy != 0 {
		t.Fatalf("summarise = %+v; this test exists because it is 0/0/0 — if that "+
			"changed, the refusal may no longer be the right answer", s)
	}
}

func TestGarbageIsNotClaimedForAnEngine(t *testing.T) {
	if got := planDocumentEngine([]byte("not json at all")); got != "" {
		t.Fatalf("planDocumentEngine = %q on unparseable input, want empty", got)
	}
}
