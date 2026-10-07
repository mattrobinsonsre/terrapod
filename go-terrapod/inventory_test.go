package terrapod

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

const inventoryItemJSON = `{"data":{"id":"invitem-1111","type":"inventory-items","attributes":{
  "name":"web-01","address":"10.0.0.4","groups":["web","env_prod"],
  "vars":{"ansible_user":"ec2-user"},
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

const resolvedJSON = `{"data":{"id":"invver-2222","type":"inventory-versions","attributes":{
  "host-count":2,"group-count":1,"produced-by":"api","produced-by-ref":"",
  "taken-at":"2026-01-01T00:00:00Z",
  "hosts":{"web-01":{"ansible_host":"10.0.0.4"},"switch1":{}},
  "groups":{"net":["switch1","web-01"]},
  "ansible-inventory":{"_meta":{"hostvars":{"web-01":{"ansible_host":"10.0.0.4"},"switch1":{}}},
    "all":{"children":["net"]},"net":{"hosts":["switch1","web-01"]}}},
  "relationships":{"inventory":{"data":{"id":"inv-3333","type":"inventories"}}}}}`

type invCaptured struct {
	method, path, query string
	attrs               map[string]any
}

// inventoryServer answers every request with (status, body) and records it.
func inventoryServer(t *testing.T, status int, body string) (*Client, *invCaptured) {
	t.Helper()
	got := &invCaptured{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method, got.path, got.query = r.Method, r.URL.Path, r.URL.RawQuery
		raw, _ := io.ReadAll(r.Body)
		got.attrs = nil
		if len(raw) > 0 {
			var doc struct {
				Data struct {
					Attributes map[string]any `json:"attributes"`
				} `json:"data"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Errorf("request body is not JSON:API: %s", raw)
			}
			got.attrs = doc.Data.Attributes
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, got
}

func invStr(s string) *string { return &s }

func TestCreateInventoryItem(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, inventoryItemJSON)

	item, err := c.CreateInventoryItem(t.Context(), "ws-abc", CreateInventoryItemRequest{
		Name:    "web-01",
		Address: "10.0.0.4",
		Groups:  []string{"web", "env_prod"},
		Vars:    map[string]string{"ansible_user": "ec2-user"},
	})
	if err != nil {
		t.Fatal(err)
	}

	if got.method != http.MethodPost ||
		got.path != "/api/v1/workspaces/ws-abc/inventory-items" {
		t.Errorf("wrong request: %s %s", got.method, got.path)
	}
	if got.attrs["name"] != "web-01" || got.attrs["address"] != "10.0.0.4" {
		t.Errorf("attrs: %#v", got.attrs)
	}
	if groups, ok := got.attrs["groups"].([]any); !ok || len(groups) != 2 {
		t.Errorf("groups not sent as a list: %#v", got.attrs["groups"])
	}
	if item.Name != "web-01" || item.Vars["ansible_user"] != "ec2-user" {
		t.Errorf("item: %+v", item)
	}
	if item.WorkspaceID != "ws-abc" {
		t.Errorf("workspace relationship not read: %+v", item)
	}
	if len(item.Groups) != 2 {
		t.Errorf("groups not read: %+v", item.Groups)
	}
}

func TestCreateInventoryItemOmitsWhatWasNotSet(t *testing.T) {
	// A host whose name already resolves needs no address, and a host in no
	// group needs no groups key. Sending empty values instead would make the
	// server store "" and [] as declarations rather than absences.
	c, got := inventoryServer(t, http.StatusCreated, inventoryItemJSON)

	if _, err := c.CreateInventoryItem(t.Context(), "ws-abc",
		CreateInventoryItemRequest{Name: "web-01"}); err != nil {
		t.Fatal(err)
	}

	for _, key := range []string{"address", "groups", "vars"} {
		if _, has := got.attrs[key]; has {
			t.Errorf("%s should be absent when unset: %#v", key, got.attrs)
		}
	}
}

func TestUpdateInventoryItemLeavesOmittedAttributesAlone(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, inventoryItemJSON)

	if _, err := c.UpdateInventoryItem(t.Context(), "invitem-1111",
		UpdateInventoryItemRequest{Address: invStr("10.0.0.9")}); err != nil {
		t.Fatal(err)
	}

	if got.method != http.MethodPatch || got.path != "/api/v1/inventory-items/invitem-1111" {
		t.Errorf("wrong request: %s %s", got.method, got.path)
	}
	if got.attrs["address"] != "10.0.0.9" {
		t.Errorf("address not sent: %#v", got.attrs)
	}
	if _, has := got.attrs["groups"]; has {
		t.Errorf("an omitted groups must not be sent, or it would clear them: %#v", got.attrs)
	}
}

func TestUpdateInventoryItemCanClearGroups(t *testing.T) {
	// The other half of the pointer semantics: a pointer to an empty slice has
	// to send `[]`, or removing a host from every group is inexpressible.
	c, got := inventoryServer(t, http.StatusOK, inventoryItemJSON)

	var none []string
	if _, err := c.UpdateInventoryItem(t.Context(), "invitem-1111",
		UpdateInventoryItemRequest{Groups: &none}); err != nil {
		t.Fatal(err)
	}

	v, ok := got.attrs["groups"].([]any)
	if !ok || len(v) != 0 {
		t.Errorf("a pointer to a nil slice should send [], sent %#v", got.attrs["groups"])
	}
}

func TestUpdateInventoryItemCanClearVars(t *testing.T) {
	// Same trap as the groups twin above, and the same fix: a pointer to a nil
	// map has to send `{}` or removing every host variable does nothing.
	c, got := inventoryServer(t, http.StatusOK, inventoryItemJSON)

	var none map[string]string
	if _, err := c.UpdateInventoryItem(t.Context(), "invitem-1111",
		UpdateInventoryItemRequest{Vars: &none}); err != nil {
		t.Fatal(err)
	}

	v, ok := got.attrs["vars"].(map[string]any)
	if !ok || len(v) != 0 {
		t.Errorf("a pointer to a nil map should send {}, sent %#v", got.attrs["vars"])
	}
}

func TestGetInventoryItem(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, inventoryItemJSON)

	if _, err := c.GetInventoryItem(t.Context(), "invitem-1111"); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodGet || got.path != "/api/v1/inventory-items/invitem-1111" {
		t.Errorf("wrong request: %s %s", got.method, got.path)
	}
}

func TestDeleteInventoryItem(t *testing.T) {
	c, got := inventoryServer(t, http.StatusNoContent, "")

	if err := c.DeleteInventoryItem(t.Context(), "invitem-1111"); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodDelete {
		t.Errorf("method = %s", got.method)
	}
}

func TestInventoryItemNotFound(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusNotFound,
		`{"errors":[{"status":"404","detail":"Inventory item not found"}],"detail":"Inventory item not found"}`)

	_, err := c.GetInventoryItem(t.Context(), "invitem-missing")
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("want NotFoundError, got %v", err)
	}
}

func TestInventoryItemDuplicateIsAConflict(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusConflict,
		`{"errors":[{"status":"409","detail":"A host named 'web-01' is already declared in this workspace"}],"detail":"A host named 'web-01' is already declared in this workspace"}`)

	_, err := c.CreateInventoryItem(t.Context(), "ws-abc",
		CreateInventoryItemRequest{Name: "web-01"})
	var cf *ConflictError
	if !errors.As(err, &cf) {
		t.Fatalf("want ConflictError, got %v", err)
	}
}

func TestListAllInventoryItemsLoopsAllPages(t *testing.T) {
	// A `for_each` over a few hundred instances is the documented shape for
	// this resource, so the collection is routinely larger than one page.
	pages := map[string]string{
		"1": `{"data":[
		  {"id":"invitem-a","type":"inventory-items","attributes":{"name":"a"}},
		  {"id":"invitem-b","type":"inventory-items","attributes":{"name":"b"}}],
		  "meta":{"pagination":{"current-page":1,"page-size":2,"total-pages":3,"total-count":5}}}`,
		"2": `{"data":[
		  {"id":"invitem-c","type":"inventory-items","attributes":{"name":"c"}},
		  {"id":"invitem-d","type":"inventory-items","attributes":{"name":"d"}}],
		  "meta":{"pagination":{"current-page":2,"page-size":2,"total-pages":3,"total-count":5}}}`,
		"3": `{"data":[
		  {"id":"invitem-e","type":"inventory-items","attributes":{"name":"e"}}],
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

	all, err := c.ListAllInventoryItems(t.Context(), "ws-abc")
	if err != nil {
		t.Fatalf("ListAllInventoryItems: %v", err)
	}
	if len(all) != 5 {
		t.Fatalf("got %d items, want 5: %+v", len(all), all)
	}
	if all[0].Name != "a" || all[4].Name != "e" {
		t.Errorf("wrong order/content: %+v", all)
	}
	if len(requested) != 3 {
		t.Errorf("requested pages %v, want exactly 3", requested)
	}
}

func TestGetResolvedInventoryKeepsVarLessHosts(t *testing.T) {
	// The whole point of the normalised shape: `switch1` has no variables, and
	// ansible's own `_meta.hostvars` would omit it. Anything enumerating the
	// host set from that would lose it silently.
	c, got := inventoryServer(t, http.StatusOK, resolvedJSON)

	version, err := c.GetResolvedInventory(t.Context(), "inv-3333")
	if err != nil {
		t.Fatal(err)
	}

	if got.path != "/api/v1/inventories/inv-3333/resolved" {
		t.Errorf("path = %s", got.path)
	}
	if len(version.Hosts) != 2 {
		t.Fatalf("hosts = %#v, want both", version.Hosts)
	}
	vars, ok := version.Hosts["switch1"]
	if !ok {
		t.Fatal("a var-less host must still appear in Hosts")
	}
	if len(vars) != 0 {
		t.Errorf("switch1 vars = %#v, want empty", vars)
	}
	if version.Hosts["web-01"]["ansible_host"] != "10.0.0.4" {
		t.Errorf("web-01 vars = %#v", version.Hosts["web-01"])
	}
	if len(version.Groups["net"]) != 2 {
		t.Errorf("groups = %#v", version.Groups)
	}
	if version.TakenAt != "2026-01-01T00:00:00Z" {
		t.Errorf("taken-at not read: %q", version.TakenAt)
	}
	if version.AnsibleInventory == nil {
		t.Error("the rendered ansible shape should be carried through")
	}
	if version.InventoryID != "inv-3333" {
		t.Errorf("inventory relationship not read: %q", version.InventoryID)
	}
}

func TestResolvedInventoryRefusalIsAConflict(t *testing.T) {
	// A source needing ansible, with no snapshot yet. The API refuses rather
	// than resolving what it can, so the SDK surfaces that as a conflict rather
	// than an empty inventory.
	c, _ := inventoryServer(t, http.StatusConflict,
		`{"errors":[{"status":"409","detail":"contains a source the API cannot resolve"}],"detail":"contains a source the API cannot resolve"}`)

	_, err := c.GetResolvedInventory(t.Context(), "inv-3333")
	var cf *ConflictError
	if !errors.As(err, &cf) {
		t.Fatalf("want ConflictError, got %v", err)
	}
}

func TestRecordInventoryVersionSendsEmptyMapsNotNull(t *testing.T) {
	// An empty resolution is a real answer -- a workspace whose hosts have all
	// been destroyed. Sending null would make the server read it as absent.
	c, got := inventoryServer(t, http.StatusCreated, resolvedJSON)

	if _, err := c.RecordInventoryVersion(t.Context(), "inv-3333",
		RecordInventoryVersionRequest{}); err != nil {
		t.Fatal(err)
	}

	if got.path != "/api/v1/inventories/inv-3333/versions" {
		t.Errorf("path = %s", got.path)
	}
	for _, key := range []string{"hosts", "groups"} {
		v, has := got.attrs[key]
		if !has {
			t.Errorf("%s must be sent even when empty: %#v", key, got.attrs)
			continue
		}
		if _, ok := v.(map[string]any); !ok {
			t.Errorf("%s = %#v, want an object", key, v)
		}
	}
}

func TestRecordInventoryVersionSendsTheResolution(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, resolvedJSON)

	_, err := c.RecordInventoryVersion(t.Context(), "inv-3333", RecordInventoryVersionRequest{
		Hosts:  map[string]map[string]any{"h1": {"ansible_port": 2222}},
		Groups: map[string][]string{"web": {"h1"}},
	})
	if err != nil {
		t.Fatal(err)
	}

	hosts, ok := got.attrs["hosts"].(map[string]any)
	if !ok {
		t.Fatalf("hosts = %#v", got.attrs["hosts"])
	}
	h1, ok := hosts["h1"].(map[string]any)
	if !ok || h1["ansible_port"] != float64(2222) {
		t.Errorf("a runner may post a non-string variable: %#v", hosts)
	}
}

func TestResolveInventory(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, resolvedJSON)

	if _, err := c.ResolveInventory(t.Context(), "inv-3333"); err != nil {
		t.Fatal(err)
	}
	if got.method != http.MethodPost ||
		got.path != "/api/v1/inventories/inv-3333/actions/resolve" {
		t.Errorf("wrong request: %s %s", got.method, got.path)
	}
}

func TestPreviewInventoryLimit(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK,
		`{"data":{"id":"invver-2222","type":"inventory-limit-previews","attributes":{
		  "limit":"web:!host2","hosts":["host1"],"host-count":1,"of-host-count":3,
		  "taken-at":"2026-01-01T00:00:00Z"}}}`)

	preview, err := c.PreviewInventoryLimit(t.Context(), "inv-3333", "web:!host2")
	if err != nil {
		t.Fatal(err)
	}

	if got.attrs["limit"] != "web:!host2" {
		t.Errorf("limit not sent: %#v", got.attrs)
	}
	if preview.HostCount != 1 || preview.OfHostCount != 3 {
		t.Errorf("counts: %+v", preview)
	}
	if len(preview.Hosts) != 1 || preview.Hosts[0] != "host1" {
		t.Errorf("hosts: %+v", preview.Hosts)
	}
}

func TestPreviewInventoryLimitRefusesARegex(t *testing.T) {
	// The API refuses rather than matching nothing; the SDK must surface that
	// as a validation error, not an empty target set.
	c, _ := inventoryServer(t, http.StatusUnprocessableEntity,
		`{"errors":[{"status":"422","detail":"is a regular expression, which this preview does not expand"}],"detail":"is a regular expression, which this preview does not expand"}`)

	_, err := c.PreviewInventoryLimit(t.Context(), "inv-3333", "~web.*")
	var ve *ValidationError
	if !errors.As(err, &ve) {
		t.Fatalf("want ValidationError, got %v", err)
	}
}

func TestInventoryCRUD(t *testing.T) {
	body := `{"data":{"id":"inv-3333","type":"inventories","attributes":{
	  "name":"default","description":"","api-resolvable":true,
	  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
	  "relationships":{"workspace":{"data":{"id":"ws-abc","type":"workspaces"}}}}}`

	c, got := inventoryServer(t, http.StatusCreated, body)
	inventory, err := c.CreateInventory(t.Context(), "ws-abc",
		CreateInventoryRequest{Name: "default"})
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/api/v1/workspaces/ws-abc/inventories" {
		t.Errorf("path = %s", got.path)
	}
	if !inventory.APIResolvable {
		t.Error("api-resolvable should be read: it is why a snapshot may be stale")
	}
	if inventory.WorkspaceID != "ws-abc" {
		t.Errorf("workspace relationship: %q", inventory.WorkspaceID)
	}

	c2, got2 := inventoryServer(t, http.StatusNoContent, "")
	if err := c2.DeleteInventory(t.Context(), "inv-3333"); err != nil {
		t.Fatal(err)
	}
	if got2.method != http.MethodDelete || got2.path != "/api/v1/inventories/inv-3333" {
		t.Errorf("wrong request: %s %s", got2.method, got2.path)
	}
}

func TestInventoryCarriesItsSources(t *testing.T) {
	// The fixture that was missing. `Inventory.Sources` was a declared field
	// the parser never populated -- the server emitted the list under a
	// different key and no test fixture contained a source at all, so the field
	// was permanently nil and nothing said so. A fixture holding only the shapes
	// already handled cannot catch that.
	body := `{"data":{"id":"inv-3333","type":"inventories","attributes":{
	  "name":"default","api-resolvable":false,
	  "sources":[
	    {"id":"invsrc-a","position":0,"kind":"terraform","config":{},"api-resolvable":true,
	     "created-at":"2026-01-01T00:00:00Z"},
	    {"id":"invsrc-b","position":1,"kind":"ini","config":{"path":"inventory/hosts"},
	     "api-resolvable":false,"created-at":"2026-01-01T00:00:00Z"}]}}}`

	c, _ := inventoryServer(t, http.StatusOK, body)
	inventory, err := c.GetInventory(t.Context(), "inv-3333")
	if err != nil {
		t.Fatal(err)
	}

	if len(inventory.Sources) != 2 {
		t.Fatalf("sources = %#v", inventory.Sources)
	}
	// Position is the `-i` ordering, and it is what decides which source wins a
	// conflicting host variable -- so a zero here is not a cosmetic loss.
	if inventory.Sources[0].Position != 0 || inventory.Sources[1].Position != 1 {
		t.Errorf("positions: %d, %d",
			inventory.Sources[0].Position, inventory.Sources[1].Position)
	}
	if inventory.Sources[0].Kind != "terraform" || inventory.Sources[1].Kind != "ini" {
		t.Errorf("kinds: %q, %q", inventory.Sources[0].Kind, inventory.Sources[1].Kind)
	}
	// Per-source, not just the inventory's rolled-up value: this is what lets a
	// caller say WHICH source is why a snapshot is stale.
	if !inventory.Sources[0].APIResolvable || inventory.Sources[1].APIResolvable {
		t.Error("per-source api-resolvable should differ in this fixture")
	}
	if inventory.Sources[1].Config["path"] != "inventory/hosts" {
		t.Errorf("config: %#v", inventory.Sources[1].Config)
	}
	if inventory.Sources[0].ID != "invsrc-a" {
		t.Errorf("id: %q", inventory.Sources[0].ID)
	}
}

func TestAnInventoryWithNoSourcesParsesCleanly(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK,
		`{"data":{"id":"inv-3333","type":"inventories","attributes":{"name":"default"}}}`)
	inventory, err := c.GetInventory(t.Context(), "inv-3333")
	if err != nil {
		t.Fatal(err)
	}
	if inventory.Sources != nil {
		t.Errorf("sources = %#v", inventory.Sources)
	}
}

func TestListInventoryVersionsNewestFirst(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK, `{"data":[
	  {"id":"invver-b","type":"inventory-versions","attributes":{"host-count":3,"produced-by":"runner","taken-at":"2026-01-02T00:00:00Z"}},
	  {"id":"invver-a","type":"inventory-versions","attributes":{"host-count":2,"produced-by":"api","taken-at":"2026-01-01T00:00:00Z"}}]}`)

	versions, err := c.ListInventoryVersions(t.Context(), "inv-3333")
	if err != nil {
		t.Fatal(err)
	}
	if len(versions) != 2 {
		t.Fatalf("got %d", len(versions))
	}
	if versions[0].ProducedBy != "runner" || versions[0].HostCount != 3 {
		t.Errorf("first: %+v", versions[0])
	}
	// A list omits contents; reading one snapshot is how they are fetched.
	if versions[0].Hosts != nil {
		t.Errorf("a list should not carry contents: %#v", versions[0].Hosts)
	}
}

func TestListInventoriesAndItems(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK, `{"data":[
	  {"id":"inv-1","type":"inventories","attributes":{"name":"default","api-resolvable":true}}]}`)
	inventories, err := c.ListInventories(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(inventories) != 1 || inventories[0].Name != "default" {
		t.Errorf("inventories: %+v", inventories)
	}

	c2, _ := inventoryServer(t, http.StatusOK, `{"data":[
	  {"id":"invitem-1","type":"inventory-items","attributes":{"name":"web-01","groups":["web"]}}]}`)
	items, err := c2.ListInventoryItems(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Name != "web-01" {
		t.Errorf("items: %+v", items)
	}
}

func TestAWorkspaceThatDeclaresNothingHasNoInventories(t *testing.T) {
	// Keyed on data rather than a flag: nothing is created until something uses
	// it, so a terraform/tofu-only deployment sees an empty list.
	c, _ := inventoryServer(t, http.StatusOK, `{"data":[]}`)

	inventories, err := c.ListInventories(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(inventories) != 0 {
		t.Errorf("want none, got %+v", inventories)
	}
}
