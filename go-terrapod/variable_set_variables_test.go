package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newVarsetVarFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/relationships/vars"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"var-aaa","type":"vars","attributes":{
			  "key":"region","value":"eu-west-1","category":"terraform","hcl":false,"sensitive":false
			}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/relationships/vars"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"var-aaa","type":"vars","attributes":{"key":"region","value":"eu-west-1","category":"terraform"}},
			  {"id":"var-bbb","type":"vars","attributes":{"key":"db_password","category":"terraform","sensitive":true}}
			]}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"var-aaa","type":"vars","attributes":{"key":"region","value":"us-east-1","category":"terraform"}}}`))
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
	return c
}

func TestCreateVarsetVariable(t *testing.T) {
	c := newVarsetVarFixture(t)
	v, err := c.CreateVarsetVariable(t.Context(), "varset-aaa", CreateVarsetVariableRequest{
		Key:      "region",
		Value:    "eu-west-1",
		Category: "terraform",
	})
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != "var-aaa" {
		t.Errorf("var: %+v", v)
	}
}

func TestGetVarsetVariable(t *testing.T) {
	c := newVarsetVarFixture(t)
	v, err := c.GetVarsetVariable(t.Context(), "varset-aaa", "var-bbb")
	if err != nil || v == nil {
		t.Fatalf("got %v / %v", v, err)
	}
	if !v.Sensitive {
		t.Errorf("expected sensitive: %+v", v)
	}
}

func TestUpdateVarsetVariable(t *testing.T) {
	c := newVarsetVarFixture(t)
	val := "us-east-1"
	v, err := c.UpdateVarsetVariable(t.Context(), "varset-aaa", "var-aaa", UpdateVarsetVariableRequest{
		Value: &val,
	})
	if err != nil {
		t.Fatal(err)
	}
	if v.Value != "us-east-1" {
		t.Errorf("var: %+v", v)
	}
}

func TestDeleteVarsetVariable(t *testing.T) {
	c := newVarsetVarFixture(t)
	if err := c.DeleteVarsetVariable(t.Context(), "varset-aaa", "var-aaa"); err != nil {
		t.Error(err)
	}
}
