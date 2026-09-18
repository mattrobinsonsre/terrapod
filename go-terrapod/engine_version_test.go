package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// The engine version travels under two names (#1559): `engine-version` is
// canonical, `terraform-version` is the name go-tfe uses and the API keeps
// forever. These tests pin both halves of the SDK's job — read either name and
// populate both fields, and send exactly one key however the caller spelled it.

// requestAttrs decodes the JSON:API attributes from a captured request body.
func requestAttrs(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		t.Fatalf("request body: %v", err)
	}
	return req.Data.Attributes
}

func TestWorkspaceEngineVersion_ReadsEitherName(t *testing.T) {
	// A current server sends both, always equal. A server from before the
	// rename sends only the old name — the fallback is what keeps this SDK
	// working against one, and the reason both fields are always filled is so
	// a caller reading either gets the version rather than an empty string.
	cases := []struct {
		name  string
		attrs map[string]any
		want  string
	}{
		{
			name:  "current server sends both",
			attrs: map[string]any{"engine-version": "1.12.0", "terraform-version": "1.12.0"},
			want:  "1.12.0",
		},
		{
			name:  "older server sends only the original name",
			attrs: map[string]any{"terraform-version": "1.11.4"},
			want:  "1.11.4",
		},
		{
			name:  "canonical name alone is enough",
			attrs: map[string]any{"engine-version": "3.0.0"},
			want:  "3.0.0",
		},
		{
			name:  "neither name leaves both empty",
			attrs: map[string]any{},
			want:  "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newWorkspaceFixtureServer(t)
			f.readHandler = func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "api-prod", tc.attrs)))
			}
			ws, err := f.client().GetWorkspace(t.Context(), "ws-aaa")
			if err != nil {
				t.Fatalf("GetWorkspace: %v", err)
			}
			if ws.EngineVersion != tc.want {
				t.Errorf("EngineVersion = %q, want %q", ws.EngineVersion, tc.want)
			}
			if ws.TerraformVersion != tc.want {
				t.Errorf("TerraformVersion = %q, want %q", ws.TerraformVersion, tc.want)
			}
		})
	}
}

func TestWorkspaceEngineVersion_SendsOneCanonicalKey(t *testing.T) {
	// Whichever field the caller set, exactly one key goes on the wire, and it
	// is the canonical one. Sending both would risk a pair that disagrees,
	// which the server refuses with a 422.
	cases := []struct {
		name string
		req  CreateWorkspaceRequest
		want string
	}{
		{
			name: "only the original field set still sends a version",
			req:  CreateWorkspaceRequest{Name: "w", TerraformVersion: "1.11.4"},
			want: "1.11.4",
		},
		{
			name: "canonical field is used when set",
			req:  CreateWorkspaceRequest{Name: "w", EngineVersion: "1.12.0"},
			want: "1.12.0",
		},
		{
			name: "canonical wins when a caller sets both",
			req:  CreateWorkspaceRequest{Name: "w", EngineVersion: "1.12.0", TerraformVersion: "1.11.4"},
			want: "1.12.0",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newWorkspaceFixtureServer(t)
			f.createHandler = func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusCreated)
				_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "w", nil)))
			}
			if _, err := f.client().CreateWorkspace(t.Context(), tc.req); err != nil {
				t.Fatalf("CreateWorkspace: %v", err)
			}
			attrs := requestAttrs(t, f.lastBody)
			if got := attrs["engine-version"]; got != tc.want {
				t.Errorf("engine-version = %v, want %q", got, tc.want)
			}
			if _, has := attrs["terraform-version"]; has {
				t.Errorf("both keys sent; the pair could disagree: %+v", attrs)
			}
		})
	}
}

func TestUpdateWorkspaceEngineVersion_SendsOneCanonicalKey(t *testing.T) {
	f := newWorkspaceFixtureServer(t)
	f.updateHandler = func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(minimalWorkspaceBody("ws-aaa", "w", nil)))
	}
	_, err := f.client().UpdateWorkspace(t.Context(), "ws-aaa", UpdateWorkspaceRequest{
		TerraformVersion: "1.11.4",
	})
	if err != nil {
		t.Fatalf("UpdateWorkspace: %v", err)
	}
	attrs := requestAttrs(t, f.lastBody)
	if attrs["engine-version"] != "1.11.4" {
		t.Errorf("engine-version = %v, want 1.11.4", attrs["engine-version"])
	}
	if _, has := attrs["terraform-version"]; has {
		t.Errorf("both keys sent: %+v", attrs)
	}
}

func TestRunEngineVersion_ReadsEitherNameAndSendsCanonical(t *testing.T) {
	// Runs carry the same pair for the same reason, so they get the same
	// treatment — read either, send one.
	cases := []struct {
		name     string
		respAttr map[string]any
		req      CreateRunRequest
		wantSent string
		wantRead string
	}{
		{
			name:     "older server response, original request field",
			respAttr: map[string]any{"terraform-version": "1.11.4"},
			req:      CreateRunRequest{WorkspaceID: "ws-aaa", TerraformVersion: "1.11.4"},
			wantSent: "1.11.4",
			wantRead: "1.11.4",
		},
		{
			name:     "current server response, canonical request field",
			respAttr: map[string]any{"engine-version": "1.12.0", "terraform-version": "1.12.0"},
			req:      CreateRunRequest{WorkspaceID: "ws-aaa", EngineVersion: "1.12.0"},
			wantSent: "1.12.0",
			wantRead: "1.12.0",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var body []byte
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, _ = io.ReadAll(r.Body)
				_ = r.Body.Close()
				attrs, _ := json.Marshal(tc.respAttr)
				w.Header().Set("Content-Type", "application/vnd.api+json")
				w.WriteHeader(http.StatusCreated)
				_, _ = w.Write([]byte(
					`{"data":{"id":"run-aaa","type":"runs","attributes":` + string(attrs) + `}}`))
			}))
			defer srv.Close()
			c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
			if err != nil {
				t.Fatal(err)
			}
			run, err := c.CreateRun(t.Context(), tc.req)
			if err != nil {
				t.Fatalf("CreateRun: %v", err)
			}
			if run.EngineVersion != tc.wantRead || run.TerraformVersion != tc.wantRead {
				t.Errorf("run versions = %q/%q, want %q",
					run.EngineVersion, run.TerraformVersion, tc.wantRead)
			}
			attrs := requestAttrs(t, body)
			if attrs["engine-version"] != tc.wantSent {
				t.Errorf("engine-version = %v, want %q", attrs["engine-version"], tc.wantSent)
			}
			if _, has := attrs["terraform-version"]; has {
				t.Errorf("both keys sent: %+v", attrs)
			}
		})
	}
}

func TestWorkspaceSummaryEngineVersion_ReadsEitherName(t *testing.T) {
	// WorkspaceSummary is decoded straight from JSON rather than through the
	// JSON:API resource helpers, so it needs its own fallback to stay usable
	// against a server that only knows the original name.
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{name: "both names", raw: `{"engine-version":"1.12.0","terraform-version":"1.12.0"}`, want: "1.12.0"},
		{name: "original name only", raw: `{"terraform-version":"1.11.4"}`, want: "1.11.4"},
		{name: "canonical name only", raw: `{"engine-version":"1.12.0"}`, want: "1.12.0"},
		{name: "neither", raw: `{"name":"w"}`, want: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var s WorkspaceSummary
			if err := json.Unmarshal([]byte(tc.raw), &s); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			if s.EngineVersion != tc.want || s.TerraformVersion != tc.want {
				t.Errorf("versions = %q/%q, want %q", s.EngineVersion, s.TerraformVersion, tc.want)
			}
		})
	}
}

func TestWorkspaceFilterEngineVersion_SerialisesBothNames(t *testing.T) {
	// The selector's keys are snake_case, and the server normalises the
	// original name onto the canonical one. A caller that still sets only the
	// original field keeps working — including against an older server, which
	// would reject the canonical key as unknown.
	b, err := json.Marshal(WorkspaceFilter{TerraformVersion: "1.11.4"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["terraform_version"] != "1.11.4" {
		t.Errorf("terraform_version = %v", got["terraform_version"])
	}
	if _, has := got["engine_version"]; has {
		t.Errorf("engine_version invented from an unset field: %+v", got)
	}

	b, err = json.Marshal(WorkspaceFilter{EngineVersion: "1.12.0"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	got = nil
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got["engine_version"] != "1.12.0" {
		t.Errorf("engine_version = %v", got["engine_version"])
	}
	if _, has := got["terraform_version"]; has {
		t.Errorf("terraform_version invented from an unset field: %+v", got)
	}
}
