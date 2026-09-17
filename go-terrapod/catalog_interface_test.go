package terrapod

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// GetCatalogItemInterface (#1585): the inputs and outputs of the module version
// a catalog item resolves to.

func catalogInterfaceClient(t *testing.T, status int, body string) (*Client, *string) {
	t.Helper()
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &gotPath
}

func TestGetCatalogItemInterface(t *testing.T) {
	c, path := catalogInterfaceClient(t, http.StatusOK, `{"data":{"id":"ci-1","type":"catalog-item-interfaces","attributes":{
	  "resolved-version":"1.2.0",
	  "inputs":[
	    {"name":"cidr","type":"string","description":"VPC CIDR","default":null,"required":true,"sensitive":false},
	    {"name":"tags","type":"map(string)","description":"","default":"{}","required":false,"sensitive":false}],
	  "outputs":[{"name":"vpc_id","description":"The VPC","sensitive":false}]}}}`)

	iface, err := c.GetCatalogItemInterface(t.Context(), "ci-1")
	if err != nil {
		t.Fatal(err)
	}
	if *path != "/api/terrapod/v1/catalog-items/ci-1/interface" {
		t.Errorf("requested %s", *path)
	}
	if iface.ResolvedVersion != "1.2.0" {
		t.Errorf("resolved version = %q", iface.ResolvedVersion)
	}
	if len(iface.Inputs) != 2 || iface.Inputs[0]["name"] != "cidr" || iface.Inputs[0]["required"] != true {
		t.Errorf("inputs = %+v", iface.Inputs)
	}
	if iface.Inputs[1]["default"] != "{}" {
		t.Errorf("a default should come back as the registry stored it: %+v", iface.Inputs[1])
	}
	if len(iface.Outputs) != 1 || iface.Outputs[0]["name"] != "vpc_id" {
		t.Errorf("outputs = %+v", iface.Outputs)
	}
}

func TestGetCatalogItemInterfaceBeforeAnyVersionIsUploaded(t *testing.T) {
	c, _ := catalogInterfaceClient(t, http.StatusOK, `{"data":{"id":"ci-1","type":"catalog-item-interfaces",
	  "attributes":{"resolved-version":null,"inputs":null,"outputs":null}}}`)

	iface, err := c.GetCatalogItemInterface(t.Context(), "ci-1")
	if err != nil {
		t.Fatal(err)
	}
	if iface.ResolvedVersion != "" || iface.Inputs != nil || iface.Outputs != nil {
		t.Errorf("want the empty interface, got %+v", iface)
	}
}

func TestGetCatalogItemInterfaceCarriesTheParseError(t *testing.T) {
	c, _ := catalogInterfaceClient(t, http.StatusOK, `{"data":{"id":"ci-1","type":"catalog-item-interfaces",
	  "attributes":{"resolved-version":"1.2.0","inputs":[],"outputs":[],"interface-error":"variables.tf: invalid HCL"}}}`)

	iface, err := c.GetCatalogItemInterface(t.Context(), "ci-1")
	if err != nil {
		t.Fatal(err)
	}
	if iface.InterfaceError != "variables.tf: invalid HCL" {
		t.Errorf("interface error = %q", iface.InterfaceError)
	}
}

func TestGetCatalogItemInterfaceNullErrorReadsAsEmpty(t *testing.T) {
	c, _ := catalogInterfaceClient(t, http.StatusOK, `{"data":{"id":"ci-1","type":"catalog-item-interfaces",
	  "attributes":{"resolved-version":"1.2.0","inputs":[],"outputs":[],"interface-error":null}}}`)

	iface, err := c.GetCatalogItemInterface(t.Context(), "ci-1")
	if err != nil {
		t.Fatal(err)
	}
	if iface.InterfaceError != "" {
		t.Errorf("interface error = %q", iface.InterfaceError)
	}
}

func TestGetCatalogItemInterfaceNotFound(t *testing.T) {
	c, _ := catalogInterfaceClient(t, http.StatusNotFound,
		`{"errors":[{"status":"404","detail":"catalog item not found"}],"detail":"catalog item not found"}`)

	if _, err := c.GetCatalogItemInterface(t.Context(), "ci-x"); !IsNotFound(err) {
		t.Errorf("want a not-found error, got %v", err)
	}
}
