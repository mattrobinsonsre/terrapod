package vcs_connection

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// baseModel is a connection whose reach/scope attributes are all unset, which
// is the shape a config that mentions none of them produces.
func baseModel() *vcsConnectionModel {
	return &vcsConnectionModel{
		Name:                types.StringValue("github-prod"),
		Provider:            types.StringValue("github"),
		ServerURL:           types.StringNull(),
		GithubAppID:         types.Int64Null(),
		PrivateKey:          types.StringNull(),
		Token:               types.StringNull(),
		WebhookSecret:       types.StringNull(),
		OwnerEmail:          types.StringNull(),
		Labels:              types.MapNull(types.StringType),
		AllowedRepositories: types.ListNull(types.StringType),
	}
}

func mustList(t *testing.T, vals ...string) types.List {
	t.Helper()
	elems := make([]types.String, 0, len(vals))
	for _, v := range vals {
		elems = append(elems, types.StringValue(v))
	}
	l, d := types.ListValueFrom(context.Background(), types.StringType, elems)
	if d.HasError() {
		t.Fatalf("building list: %v", d)
	}
	return l
}

func mustMap(t *testing.T, kv map[string]string) types.Map {
	t.Helper()
	m, d := types.MapValueFrom(context.Background(), types.StringType, kv)
	if d.HasError() {
		t.Fatalf("building map: %v", d)
	}
	return m
}

// ── Create ───────────────────────────────────────────────────────────

func TestCreateRequestOmitsUnsetReachFields(t *testing.T) {
	req := buildCreateVCSConnectionRequest(baseModel())
	if req.OwnerEmail != "" {
		t.Errorf("owner email = %q, want empty", req.OwnerEmail)
	}
	if req.Labels != nil {
		t.Errorf("labels = %v, want nil so the SDK omits the key", req.Labels)
	}
	if req.AllowedRepositories != nil {
		t.Errorf("allowed repositories = %v, want nil so the SDK omits the key", req.AllowedRepositories)
	}
}

func TestCreateRequestCarriesReachFields(t *testing.T) {
	m := baseModel()
	m.OwnerEmail = types.StringValue("platform@example.com")
	m.Labels = mustMap(t, map[string]string{"team": "platform"})
	m.AllowedRepositories = mustList(t, "example-org/infra-*", "example-org/app")

	req := buildCreateVCSConnectionRequest(m)
	if req.OwnerEmail != "platform@example.com" {
		t.Errorf("owner email = %q", req.OwnerEmail)
	}
	if len(req.Labels) != 1 || req.Labels["team"] != "platform" {
		t.Errorf("labels = %v", req.Labels)
	}
	if len(req.AllowedRepositories) != 2 ||
		req.AllowedRepositories[0] != "example-org/infra-*" ||
		req.AllowedRepositories[1] != "example-org/app" {
		t.Errorf("allowed repositories = %v", req.AllowedRepositories)
	}
}

// ── Update: the clear-vs-omit distinction ────────────────────────────

// A null or unknown planned value must leave the stored value alone. This is
// what stops an unrelated change from silently widening a repository allowlist
// somebody set deliberately.
func TestUpdateRequestOmitsUnsetReachFields(t *testing.T) {
	req := buildUpdateVCSConnectionRequest(baseModel())
	if req.OwnerEmail != nil {
		t.Errorf("owner email = %v, want nil (leave alone)", *req.OwnerEmail)
	}
	if req.Labels != nil {
		t.Errorf("labels = %v, want nil (leave alone)", *req.Labels)
	}
	if req.AllowedRepositories != nil {
		t.Errorf("allowed repositories = %v, want nil (leave alone)", *req.AllowedRepositories)
	}
}

func TestUpdateRequestLeavesUnknownValuesAlone(t *testing.T) {
	m := baseModel()
	m.OwnerEmail = types.StringUnknown()
	m.Labels = types.MapUnknown(types.StringType)
	m.AllowedRepositories = types.ListUnknown(types.StringType)

	req := buildUpdateVCSConnectionRequest(m)
	if req.OwnerEmail != nil || req.Labels != nil || req.AllowedRepositories != nil {
		t.Errorf("an unknown value must not be sent: %+v", req)
	}
}

// Emptying the allowlist is how the scope is widened back to any repository, so
// an explicitly empty list has to reach the SDK as a non-nil empty slice. If it
// arrived as nil the key would be omitted, the server would read "leave alone",
// and removing the last pattern would appear to do nothing — the allowlist would
// be a one-way door.
func TestUpdateRequestSendsAnExplicitlyEmptyAllowlist(t *testing.T) {
	m := baseModel()
	m.AllowedRepositories = mustList(t)

	req := buildUpdateVCSConnectionRequest(m)
	if req.AllowedRepositories == nil {
		t.Fatal("an explicitly empty allowlist was omitted, so the clear would be lost")
	}
	if len(*req.AllowedRepositories) != 0 {
		t.Errorf("allowed repositories = %v, want empty", *req.AllowedRepositories)
	}
	if *req.AllowedRepositories == nil {
		t.Error("empty allowlist must be non-nil so it marshals as [] and not null")
	}
}

func TestUpdateRequestSendsAnExplicitlyEmptyLabelSet(t *testing.T) {
	m := baseModel()
	m.Labels = mustMap(t, map[string]string{})

	req := buildUpdateVCSConnectionRequest(m)
	if req.Labels == nil {
		t.Fatal("an explicitly empty label set was omitted, so the clear would be lost")
	}
	if len(*req.Labels) != 0 {
		t.Errorf("labels = %v, want empty", *req.Labels)
	}
	if *req.Labels == nil {
		t.Error("empty labels must be non-nil so they marshal as {} and not null")
	}
}

func TestUpdateRequestSendsAnExplicitlyEmptyOwner(t *testing.T) {
	m := baseModel()
	m.OwnerEmail = types.StringValue("")

	req := buildUpdateVCSConnectionRequest(m)
	if req.OwnerEmail == nil {
		t.Fatal("an explicit empty owner was omitted, so the clear would be lost")
	}
	if *req.OwnerEmail != "" {
		t.Errorf("owner email = %q, want empty", *req.OwnerEmail)
	}
}

// Update touches only the three reach/scope attributes. Everything else on this
// resource forces replacement, so including it would widen a deliberately
// narrow write — and sending a credential that the plan happens to carry would
// rotate it on an unrelated edit.
func TestUpdateRequestTouchesNothingElse(t *testing.T) {
	m := baseModel()
	m.Name = types.StringValue("github-renamed")
	m.ServerURL = types.StringValue("https://github.example.com")
	m.PrivateKey = types.StringValue("-----BEGIN RSA-----\nkey\n-----END RSA-----")
	m.Token = types.StringValue("a-token")
	m.WebhookSecret = types.StringValue("a-secret")
	m.GithubAppID = types.Int64Value(12345)
	m.OwnerEmail = types.StringValue("platform@example.com")

	req := buildUpdateVCSConnectionRequest(m)
	if req.Name != "" {
		t.Errorf("name = %q, want empty", req.Name)
	}
	if req.ServerURL != "" {
		t.Errorf("server url = %q, want empty", req.ServerURL)
	}
	if req.PrivateKey != "" || req.Token != "" {
		t.Error("a credential reached a reach/scope PATCH, which would rotate it")
	}
	if req.WebhookSecret != nil {
		t.Errorf("webhook secret = %v, want nil", *req.WebhookSecret)
	}
	if req.GithubAppID != nil {
		t.Errorf("github app id = %v, want nil", *req.GithubAppID)
	}
	if req.OwnerEmail == nil || *req.OwnerEmail != "platform@example.com" {
		t.Error("the reach fields themselves should still be sent")
	}
}

// ── Reading the server's answer back ─────────────────────────────────

func TestReadPopulatesReachFields(t *testing.T) {
	v := &terrapod.VCSConnection{
		ID: "vcs-1", Name: "github-prod", Provider: "github",
		OwnerEmail:          "platform@example.com",
		Labels:              map[string]string{"team": "platform"},
		AllowedRepositories: []string{"example-org/infra-*"},
	}
	m := baseModel()
	if d := readVCSConnectionFromSDK(context.Background(), v, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if m.OwnerEmail.ValueString() != "platform@example.com" {
		t.Errorf("owner email = %q", m.OwnerEmail.ValueString())
	}
	if len(m.Labels.Elements()) != 1 {
		t.Errorf("labels = %v", m.Labels)
	}
	if len(m.AllowedRepositories.Elements()) != 1 {
		t.Errorf("allowed repositories = %v", m.AllowedRepositories)
	}
}

// An empty value from the server must round-trip as an empty value and never as
// null. A config that asks for `allowed_repositories = []` plans as an empty
// list, so writing null back over it fails the apply with "provider produced
// inconsistent result" — which would make widening the scope impossible through
// Terraform at all.
func TestReadKeepsEmptyReachFieldsEmptyRatherThanNull(t *testing.T) {
	v := &terrapod.VCSConnection{
		ID: "vcs-1", Name: "github-prod", Provider: "github",
		OwnerEmail:          "",
		Labels:              map[string]string{},
		AllowedRepositories: []string{},
	}
	m := baseModel()
	m.AllowedRepositories = mustList(t)
	m.Labels = mustMap(t, map[string]string{})
	m.OwnerEmail = types.StringValue("")

	if d := readVCSConnectionFromSDK(context.Background(), v, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if m.AllowedRepositories.IsNull() {
		t.Error("an empty allowlist became null, which cannot match a planned []")
	}
	if m.Labels.IsNull() {
		t.Error("an empty label set became null, which cannot match a planned {}")
	}
	if m.OwnerEmail.IsNull() {
		t.Error("an empty owner became null, which cannot match a planned \"\"")
	}
}

// A server that predates the fix sends none of the three, which the SDK decodes
// as nil. The model must still come out with usable empty collections rather
// than nulls that a later apply would trip over.
func TestReadHandlesAServerThatReportsNoReachFields(t *testing.T) {
	v := &terrapod.VCSConnection{ID: "vcs-1", Name: "github-prod", Provider: "github"}
	m := baseModel()
	if d := readVCSConnectionFromSDK(context.Background(), v, m); d.HasError() {
		t.Fatalf("read: %v", d)
	}
	if m.AllowedRepositories.IsNull() || len(m.AllowedRepositories.Elements()) != 0 {
		t.Errorf("allowed repositories = %v, want a known empty list", m.AllowedRepositories)
	}
	if m.Labels.IsNull() || len(m.Labels.Elements()) != 0 {
		t.Errorf("labels = %v, want a known empty map", m.Labels)
	}
	if m.Name.ValueString() != "github-prod" {
		t.Errorf("the rest of the connection must still read, got %q", m.Name.ValueString())
	}
}
