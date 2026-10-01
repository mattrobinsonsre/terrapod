package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newVCSConnFixture(t *testing.T) (*Client, *[]byte, *string) {
	t.Helper()
	var lastBody []byte
	var lastMethod string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lastMethod = r.Method
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","status":"active","has-token":true,"github-app-id":12345}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/vcs-connections":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","has-token":true}},
			  {"id":"vcs-bbb","type":"vcs-connections","attributes":{"name":"gitlab-internal","provider":"gitlab","has-token":true}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/vcs-connections/"):
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-prod","provider":"github","has-token":true}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{"name":"github-renamed","provider":"github","has-token":true}}}`))
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &lastBody, &lastMethod
}

func TestCreateVCSConnection_Github(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	v, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:                 "github-prod",
		Provider:             "github",
		GithubAppID:          12345,
		GithubInstallationID: 67890,
		PrivateKey:           "-----BEGIN RSA-----\nkey\n-----END RSA-----",
	})
	if err != nil {
		t.Fatalf("CreateVCSConnection: %v", err)
	}
	if v.ID != "vcs-aaa" || v.Name != "github-prod" || v.Provider != "github" || v.GithubAppID != 12345 {
		t.Errorf("vcs-connection: %+v", v)
	}
	// Request body shape — private-key sent, never echoed back in
	// the response (HasToken=true indicates server has it).
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["private-key"] == nil {
		t.Errorf("private-key missing from request: %+v", req.Data.Attributes)
	}
	if !v.HasToken {
		t.Error("HasToken should be true on response")
	}
}

func TestCreateVCSConnection_Gitlab(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:      "gitlab-internal",
		Provider:  "gitlab",
		ServerURL: "https://gitlab.acme.example",
		Token:     "glpat-...",
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["token"] == nil {
		t.Error("token missing from request")
	}
	if req.Data.Attributes["server-url"] != "https://gitlab.acme.example" {
		t.Errorf("server-url: %+v", req.Data.Attributes)
	}
}

func TestGetVCSConnection(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	v, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != "vcs-aaa" {
		t.Errorf("id: %q", v.ID)
	}
}

func TestListVCSConnections(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	list, err := c.ListVCSConnections(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 || list[1].Provider != "gitlab" {
		t.Errorf("list: %+v", list)
	}
}

func TestUpdateVCSConnection_RotateCredentialOnlyWhenSet(t *testing.T) {
	// Vanilla PATCH that only renames should NOT include a private-key
	// in the body (that'd clear or rotate the existing one). The SDK
	// drops empty PrivateKey/Token from the request.
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		Name: "github-renamed",
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["name"] != "github-renamed" {
		t.Errorf("name not in body: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["private-key"]; has {
		t.Errorf("private-key leaked into rename-only request: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["token"]; has {
		t.Errorf("token leaked into rename-only request: %+v", req.Data.Attributes)
	}
}

func TestUpdateVCSConnection_RotateCredential(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		PrivateKey: "new-key",
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["private-key"] != "new-key" {
		t.Errorf("private-key not in body: %+v", req.Data.Attributes)
	}
}

func TestDeleteVCSConnection(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	if err := c.DeleteVCSConnection(t.Context(), "vcs-aaa"); err != nil {
		t.Error(err)
	}
}

func TestListAllVCSConnections_LoopsAllPages(t *testing.T) {
	// 3 pages of 2 (total 5). ListAllVCSConnections must loop on
	// meta.total-pages and return every connection, requesting exactly 3 pages.
	pages := map[string]string{
		"1": `{"data":[
		  {"id":"vcs-a","type":"vcs-connections","attributes":{"name":"a","provider":"github"}},
		  {"id":"vcs-b","type":"vcs-connections","attributes":{"name":"b","provider":"github"}}],
		  "meta":{"pagination":{"current-page":1,"page-size":2,"total-pages":3,"total-count":5}}}`,
		"2": `{"data":[
		  {"id":"vcs-c","type":"vcs-connections","attributes":{"name":"c","provider":"gitlab"}},
		  {"id":"vcs-d","type":"vcs-connections","attributes":{"name":"d","provider":"gitlab"}}],
		  "meta":{"pagination":{"current-page":2,"page-size":2,"total-pages":3,"total-count":5}}}`,
		"3": `{"data":[
		  {"id":"vcs-e","type":"vcs-connections","attributes":{"name":"e","provider":"github"}}],
		  "meta":{"pagination":{"current-page":3,"page-size":2,"total-pages":3,"total-count":5}}}`,
	}
	var requested []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page[number]")
		requested = append(requested, page)
		if got := r.URL.Query().Get("page[size]"); got != "100" {
			t.Errorf("page size = %q, want 100", got)
		}
		body, ok := pages[page]
		if !ok {
			t.Fatalf("unexpected page request: %q", page)
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	all, err := c.ListAllVCSConnections(t.Context())
	if err != nil {
		t.Fatalf("ListAllVCSConnections: %v", err)
	}
	if len(all) != 5 {
		t.Fatalf("got %d connections, want 5: %+v", len(all), all)
	}
	if all[0].ID != "vcs-a" || all[4].ID != "vcs-e" {
		t.Errorf("wrong order/content: %+v", all)
	}
	if len(requested) != 3 {
		t.Errorf("requested pages %v, want exactly 3", requested)
	}
}

// Consumption decoding (#1339). The saturation verdict and the breakdown are
// what an operator acts on, so a silently-dropped field here is the whole
// feature failing quietly.
func TestGetVCSConnection_DecodesConsumption(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{
		  "name":"github-prod","provider":"github","has-token":true,
		  "rate-limit":5000,"rate-limit-remaining":30,
		  "calls-per-hour":11400,"rate-window-minutes":60,"seconds-to-reset":1800,
		  "saturation":"will_exhaust","exhausts-in-seconds":9,
		  "top-consumers":[{"name":"org/infra","kind":"workspace","calls":9000},
		                   {"name":"default/vpc/aws","kind":"module","calls":2400}],
		  "label-totals":[{"label":"team=platform","key":"team","value":"platform","calls":9000}]
		}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	conn, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if conn.Saturation != "will_exhaust" {
		t.Errorf("saturation = %q, want will_exhaust", conn.Saturation)
	}
	if conn.CallsPerHour == nil || *conn.CallsPerHour != 11400 {
		t.Errorf("calls-per-hour = %v, want 11400", conn.CallsPerHour)
	}
	if conn.ExhaustsInSeconds == nil || *conn.ExhaustsInSeconds != 9 {
		t.Errorf("exhausts-in-seconds = %v, want 9", conn.ExhaustsInSeconds)
	}
	if len(conn.TopConsumers) != 2 {
		t.Fatalf("top-consumers = %d entries, want 2", len(conn.TopConsumers))
	}
	if conn.TopConsumers[0].Kind != "workspace" || conn.TopConsumers[0].Calls != 9000 {
		t.Errorf("first consumer = %+v", conn.TopConsumers[0])
	}
	if conn.TopConsumers[1].Kind != "module" {
		t.Errorf("second consumer kind = %q, want module", conn.TopConsumers[1].Kind)
	}
	if len(conn.LabelTotals) != 1 || conn.LabelTotals[0].Key != "team" {
		t.Errorf("label-totals = %+v", conn.LabelTotals)
	}
}

// An older server sends none of these. The connection must still decode —
// version skew across a MINOR must never turn into a client-side failure.
func TestGetVCSConnection_ConsumptionAbsentIsNotAnError(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	conn, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if conn.Saturation != "" || conn.CallsPerHour != nil {
		t.Errorf("absent consumption should stay absent, got %+v", conn)
	}
	if conn.TopConsumers != nil || conn.LabelTotals != nil {
		t.Errorf("absent breakdown should be nil, got %+v / %+v", conn.TopConsumers, conn.LabelTotals)
	}
	if conn.Name != "github-prod" {
		t.Errorf("the rest of the connection must still decode, got %q", conn.Name)
	}
}

// A remaining budget of zero is "exhausted", not "not reported" — the two mean
// opposite things and both decode through the same pointer field. Swapping
// getOptionalIntAttr for GetIntAttr, or slipping in a `!= 0` guard, silently
// turns an exhausted connection into an unmonitored-looking one; the existing
// tests cover 30 and absent, so neither would catch it (#1345).
func TestZeroRemainingDecodesAsZeroNotNil(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"vcs-1","type":"vcs-connections","attributes":{
			"name":"gh","provider":"github",
			"rate-limit":5000,"rate-limit-remaining":0,
			"calls-per-hour":4999,"saturation":"exhausted",
			"budget-window-seconds":3600,"consumers-window-total":4999}}}`))
	}))
	defer srv.Close()

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	got, err := c.GetVCSConnection(t.Context(), "vcs-1")
	if err != nil {
		t.Fatalf("GetVCSConnection: %v", err)
	}
	if got.RateLimitRemaining == nil {
		t.Fatal("remaining=0 decoded as nil — exhausted is indistinguishable from unreported")
	}
	if *got.RateLimitRemaining != 0 {
		t.Fatalf("remaining = %d, want 0", *got.RateLimitRemaining)
	}
	if got.BudgetWindowSeconds == nil || *got.BudgetWindowSeconds != 3600 {
		t.Fatalf("budget window not decoded: %v", got.BudgetWindowSeconds)
	}
	if got.ConsumersWindowTotal == nil || *got.ConsumersWindowTotal != 4999 {
		t.Fatalf("consumers total not decoded: %v", got.ConsumersWindowTotal)
	}
}

// A server that predates the window fields must decode to nil, not 0 — a
// consumer scaling a rate by a zero window would divide the utilisation away.
func TestOlderServerLeavesTheWindowFieldsNil(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"vcs-1","type":"vcs-connections","attributes":{
			"name":"gh","provider":"github","calls-per-hour":10}}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	got, err := c.GetVCSConnection(t.Context(), "vcs-1")
	if err != nil {
		t.Fatalf("GetVCSConnection: %v", err)
	}
	if got.BudgetWindowSeconds != nil {
		t.Fatalf("absent window should stay nil, got %d", *got.BudgetWindowSeconds)
	}
	if got.Saturation != "" {
		t.Fatalf("absent saturation should stay empty, got %q", got.Saturation)
	}
}

// ── Reach and scope: owner, labels, repository allowlist (GHSA-v8g7-pqrj-8mcm) ──

func TestVCSConnection_DecodesReachAndScope(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{
		  "name":"github-prod","provider":"github","has-token":true,
		  "owner-email":"platform@example.com",
		  "labels":{"team":"platform","tier":"prod"},
		  "allowed-repositories":["example-org/infra-*","example-org/app"]
		}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	conn, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatalf("GetVCSConnection: %v", err)
	}
	if conn.OwnerEmail != "platform@example.com" {
		t.Errorf("owner-email = %q", conn.OwnerEmail)
	}
	if len(conn.Labels) != 2 || conn.Labels["team"] != "platform" || conn.Labels["tier"] != "prod" {
		t.Errorf("labels = %+v", conn.Labels)
	}
	if len(conn.AllowedRepositories) != 2 ||
		conn.AllowedRepositories[0] != "example-org/infra-*" ||
		conn.AllowedRepositories[1] != "example-org/app" {
		t.Errorf("allowed-repositories = %+v", conn.AllowedRepositories)
	}
}

// An empty allowlist means "any repository", so it must decode to a connection
// that is unrestricted — not be mistaken for a scope that failed to decode. The
// server always sends the key, so this is the ordinary shape of an unscoped
// connection rather than an edge case.
func TestVCSConnection_EmptyAllowlistMeansAnyRepository(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = w.Write([]byte(`{"data":{"id":"vcs-aaa","type":"vcs-connections","attributes":{
		  "name":"github-prod","provider":"github",
		  "owner-email":"","labels":{},"allowed-repositories":[]}}}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	conn, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatalf("GetVCSConnection: %v", err)
	}
	if len(conn.AllowedRepositories) != 0 {
		t.Errorf("allowed-repositories should be empty, got %+v", conn.AllowedRepositories)
	}
	if len(conn.Labels) != 0 {
		t.Errorf("labels should be empty, got %+v", conn.Labels)
	}
	if conn.OwnerEmail != "" {
		t.Errorf("owner-email should be empty, got %q", conn.OwnerEmail)
	}
	if conn.Name != "github-prod" {
		t.Errorf("the rest of the connection must still decode, got %q", conn.Name)
	}
}

// A server that predates the fix sends none of the three. Version skew across a
// MINOR must not become a client-side failure.
func TestVCSConnection_ReachAbsentIsNotAnError(t *testing.T) {
	c, _, _ := newVCSConnFixture(t)
	conn, err := c.GetVCSConnection(t.Context(), "vcs-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if conn.OwnerEmail != "" || conn.Labels != nil || conn.AllowedRepositories != nil {
		t.Errorf("absent reach fields should stay zero, got %+v", conn)
	}
	if conn.Name != "github-prod" {
		t.Errorf("the rest of the connection must still decode, got %q", conn.Name)
	}
}

func TestCreateVCSConnection_SendsReachAndScope(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:                "github-prod",
		Provider:            "github",
		GithubAppID:         12345,
		PrivateKey:          "-----BEGIN RSA-----\nkey\n-----END RSA-----",
		OwnerEmail:          "platform@example.com",
		Labels:              map[string]string{"team": "platform"},
		AllowedRepositories: []string{"example-org/infra-*"},
	})
	if err != nil {
		t.Fatalf("CreateVCSConnection: %v", err)
	}
	attrs := vcsConnReqAttrs(t, *lastBody)
	if attrs["owner-email"] != "platform@example.com" {
		t.Errorf("owner-email not sent: %+v", attrs)
	}
	labels, ok := attrs["labels"].(map[string]any)
	if !ok || labels["team"] != "platform" {
		t.Errorf("labels not sent: %+v", attrs["labels"])
	}
	repos, ok := attrs["allowed-repositories"].([]any)
	if !ok || len(repos) != 1 || repos[0] != "example-org/infra-*" {
		t.Errorf("allowed-repositories not sent: %+v", attrs["allowed-repositories"])
	}
}

// Create omits what the caller did not set, so the server's defaults apply
// rather than the SDK asserting an empty owner/labels/allowlist on its behalf.
func TestCreateVCSConnection_OmitsUnsetReachFields(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name:     "github-prod",
		Provider: "github",
	})
	if err != nil {
		t.Fatal(err)
	}
	attrs := vcsConnReqAttrs(t, *lastBody)
	for _, k := range []string{"owner-email", "labels", "allowed-repositories"} {
		if _, has := attrs[k]; has {
			t.Errorf("%s should be omitted when unset: %+v", k, attrs)
		}
	}
}

// The clear-vs-omit distinction, which is the whole reason these are pointers.
// A rename must not clear the owner, the labels or the allowlist.
func TestUpdateVCSConnection_OmitsReachFieldsLeftNil(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		Name: "github-renamed",
	})
	if err != nil {
		t.Fatal(err)
	}
	attrs := vcsConnReqAttrs(t, *lastBody)
	for _, k := range []string{"owner-email", "labels", "allowed-repositories"} {
		if _, has := attrs[k]; has {
			t.Errorf("%s leaked into a rename-only request, which would overwrite it: %+v", k, attrs)
		}
	}
}

func TestUpdateVCSConnection_SetsReachAndScope(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	owner := "platform@example.com"
	labels := map[string]string{"team": "platform"}
	repos := []string{"example-org/infra-*"}
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		OwnerEmail:          &owner,
		Labels:              &labels,
		AllowedRepositories: &repos,
	})
	if err != nil {
		t.Fatal(err)
	}
	attrs := vcsConnReqAttrs(t, *lastBody)
	if attrs["owner-email"] != owner {
		t.Errorf("owner-email = %v", attrs["owner-email"])
	}
	if m, ok := attrs["labels"].(map[string]any); !ok || m["team"] != "platform" {
		t.Errorf("labels = %+v", attrs["labels"])
	}
	if s, ok := attrs["allowed-repositories"].([]any); !ok || len(s) != 1 {
		t.Errorf("allowed-repositories = %+v", attrs["allowed-repositories"])
	}
}

// Clearing. An explicitly empty value must reach the wire as an empty
// object/array and never as null or an omitted key: an omitted key means
// "leave alone", so a clear that marshalled away would silently leave the old
// allowlist in force. Removing the last pattern has to restore "any
// repository", or the allowlist is a one-way door.
func TestUpdateVCSConnection_ExplicitlyEmptyClears(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	owner := ""
	labels := map[string]string{}
	repos := []string{}
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		OwnerEmail:          &owner,
		Labels:              &labels,
		AllowedRepositories: &repos,
	})
	if err != nil {
		t.Fatal(err)
	}
	// Inspect the raw body, not only the decoded map: `null` and `[]` both
	// decode to a present key, and only one of them is right.
	raw := string(*lastBody)
	if !strings.Contains(raw, `"allowed-repositories":[]`) {
		t.Errorf("empty allowlist did not marshal as []: %s", raw)
	}
	if !strings.Contains(raw, `"labels":{}`) {
		t.Errorf("empty labels did not marshal as {}: %s", raw)
	}
	attrs := vcsConnReqAttrs(t, *lastBody)
	if v, has := attrs["owner-email"]; !has || v != "" {
		t.Errorf("owner-email should be present and empty, got %v (present=%v)", v, has)
	}
}

// A nil map or slice behind a non-nil pointer is still a clear, not an
// omission — it is the shape a caller gets from `var repos []string;
// req.AllowedRepositories = &repos`, and marshalling it as null would leave
// the reader of the wire unable to tell a clear from nothing sent.
func TestUpdateVCSConnection_NilBehindPointerStillClears(t *testing.T) {
	c, lastBody, _ := newVCSConnFixture(t)
	var repos []string
	var labels map[string]string
	_, err := c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{
		Labels:              &labels,
		AllowedRepositories: &repos,
	})
	if err != nil {
		t.Fatal(err)
	}
	raw := string(*lastBody)
	if strings.Contains(raw, `"allowed-repositories":null`) {
		t.Errorf("a nil slice behind a pointer marshalled as null: %s", raw)
	}
	if !strings.Contains(raw, `"allowed-repositories":[]`) {
		t.Errorf("expected [], got: %s", raw)
	}
	if !strings.Contains(raw, `"labels":{}`) {
		t.Errorf("expected {}, got: %s", raw)
	}
}

// Error path: the server rejects a reserved label key with 422, and the SDK
// must surface that as a ValidationError carrying the server's detail rather
// than a bare APIError.
func TestUpdateVCSConnection_ReservedLabelIsAValidationError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"status":"422","detail":"label key 'owner' is reserved"}],` +
			`"detail":"label key 'owner' is reserved"}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	labels := map[string]string{"owner": "someone@example.com"}
	_, err = c.UpdateVCSConnection(t.Context(), "vcs-aaa", UpdateVCSConnectionRequest{Labels: &labels})
	if err == nil {
		t.Fatal("expected an error for a reserved label key")
	}
	if !IsValidation(err) {
		t.Fatalf("expected a ValidationError, got %T: %v", err, err)
	}
	if !strings.Contains(err.Error(), "reserved") {
		t.Errorf("the server's detail should survive: %v", err)
	}
}

func TestCreateVCSConnection_BadAllowlistIsAValidationError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"errors":[{"status":"422",` +
			`"detail":"allowed-repositories must be a list of strings"}]}`))
	}))
	defer srv.Close()
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	_, err = c.CreateVCSConnection(t.Context(), CreateVCSConnectionRequest{
		Name: "gh", Provider: "github",
		AllowedRepositories: []string{"example-org/infra-*"},
	})
	if !IsValidation(err) {
		t.Fatalf("expected a ValidationError, got %T: %v", err, err)
	}
}

// vcsConnReqAttrs decodes the attributes of a captured JSON:API request body.
func vcsConnReqAttrs(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		t.Fatalf("unmarshal request body: %v (%s)", err, body)
	}
	return req.Data.Attributes
}
