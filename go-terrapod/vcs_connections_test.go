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
