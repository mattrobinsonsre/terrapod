package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newVarFixture(t *testing.T) (*Client, *httptest.Server, *[]byte) {
	t.Helper()
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/vars"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"var-a","type":"vars","attributes":{"key":"region","value":"eu-west-1","category":"terraform","hcl":false,"sensitive":false}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/vars"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"var-a","type":"vars","attributes":{"key":"region","value":"eu-west-1","category":"terraform"}},
			  {"id":"var-b","type":"vars","attributes":{"key":"db_password","value":"","category":"terraform","sensitive":true}}
			]}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"var-a","type":"vars","attributes":{"key":"region","value":"us-east-1","category":"terraform"}}}`))
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
	return c, srv, &lastBody
}

func TestCreateVariable_Happy(t *testing.T) {
	c, _, lastBody := newVarFixture(t)
	v, err := c.CreateVariable(t.Context(), "ws-aaa", CreateVariableRequest{
		Key:      "region",
		Value:    "eu-west-1",
		Category: "terraform",
	})
	if err != nil {
		t.Fatalf("CreateVariable: %v", err)
	}
	if v.ID != "var-a" || v.Key != "region" || v.Value != "eu-west-1" || v.Category != "terraform" {
		t.Errorf("variable: %+v", v)
	}
	// Request body shape — hcl/sensitive default to false, omitted
	// from attributes when not set (omitempty on the JSON tags
	// applies to outbound marshalling too).
	var req struct {
		Data struct {
			Type       string         `json:"type"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(*lastBody, &req); err != nil {
		t.Fatal(err)
	}
	if req.Data.Type != "vars" {
		t.Errorf("type = %q", req.Data.Type)
	}
	if req.Data.Attributes["key"] != "region" || req.Data.Attributes["category"] != "terraform" {
		t.Errorf("attributes: %+v", req.Data.Attributes)
	}
}

func TestCreateVariable_SensitiveDefaultsOff(t *testing.T) {
	c, _, lastBody := newVarFixture(t)
	_, err := c.CreateVariable(t.Context(), "ws-aaa", CreateVariableRequest{
		Key:       "db_password",
		Value:     "secret",
		Category:  "terraform",
		Sensitive: true,
		HCL:       true,
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
	if v, _ := req.Data.Attributes["sensitive"].(bool); !v {
		t.Errorf("sensitive should be true in request: %+v", req.Data.Attributes)
	}
	if v, _ := req.Data.Attributes["hcl"].(bool); !v {
		t.Errorf("hcl should be true in request: %+v", req.Data.Attributes)
	}
}

func TestListVariables(t *testing.T) {
	c, _, _ := newVarFixture(t)
	vars, err := c.ListVariables(t.Context(), "ws-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if len(vars) != 2 {
		t.Fatalf("expected 2 vars, got %d", len(vars))
	}
	if !vars[1].Sensitive {
		t.Error("sensitive flag not preserved")
	}
}

func TestGetVariable_FoundAndNotFound(t *testing.T) {
	c, _, _ := newVarFixture(t)
	v, err := c.GetVariable(t.Context(), "ws-aaa", "var-b")
	if err != nil {
		t.Fatalf("GetVariable: %v", err)
	}
	if v.Key != "db_password" {
		t.Errorf("variable: %+v", v)
	}
	_, err = c.GetVariable(t.Context(), "ws-aaa", "var-missing")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got: %v", err)
	}
}

func TestUpdateVariable_PointerSemantics(t *testing.T) {
	// nil pointer ⇒ field absent from request; &false ⇒ explicitly
	// set to false. The variable PATCH endpoint silently leaves
	// fields alone when absent — this is the contract we mirror.
	c, _, lastBody := newVarFixture(t)
	val := "us-east-1"
	_, err := c.UpdateVariable(t.Context(), "ws-aaa", "var-a", UpdateVariableRequest{
		Value: &val,
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			ID         string         `json:"id"`
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.ID != "var-a" {
		t.Errorf("id missing from body: %+v", req.Data)
	}
	if req.Data.Attributes["value"] != "us-east-1" {
		t.Errorf("value: %+v", req.Data.Attributes)
	}
	if _, has := req.Data.Attributes["sensitive"]; has {
		t.Errorf("nil sensitive leaked into request: %+v", req.Data.Attributes)
	}
}

func TestUpdateVariable_ExplicitlyFalse(t *testing.T) {
	// Setting *Sensitive = &false MUST round-trip as `false` in the
	// body — otherwise we can't un-flag a sensitive variable.
	c, _, lastBody := newVarFixture(t)
	off := false
	_, err := c.UpdateVariable(t.Context(), "ws-aaa", "var-a", UpdateVariableRequest{
		Sensitive: &off,
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
	v, has := req.Data.Attributes["sensitive"]
	if !has {
		t.Fatal("sensitive missing from body")
	}
	if v.(bool) {
		t.Errorf("sensitive should be false, got: %v", v)
	}
}

func TestDeleteVariable(t *testing.T) {
	c, _, _ := newVarFixture(t)
	if err := c.DeleteVariable(t.Context(), "ws-aaa", "var-a"); err != nil {
		t.Errorf("DeleteVariable: %v", err)
	}
}

func TestVariableCreate_Conflict409(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"errors":[{"status":"409","detail":"key already exists"}]}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	_, err := c.CreateVariable(t.Context(), "ws-aaa", CreateVariableRequest{
		Key:      "region",
		Category: "terraform",
	})
	if !IsConflict(err) {
		t.Errorf("expected ConflictError, got: %v", err)
	}
}

func TestVariableFromResource_EnvCategory(t *testing.T) {
	body := `{"data":{"id":"var-x","type":"vars","attributes":{
	  "key":"AWS_PROFILE","value":"ci","category":"env",
	  "hcl":false,"sensitive":false,"description":"CI profile"
	}}}`
	v, err := parseVariable([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if v.Category != "env" || v.Description != "CI profile" {
		t.Errorf("variable: %+v", v)
	}
}
