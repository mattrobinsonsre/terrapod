package terrapod

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Real blobs, so the fixtures are the shape the server's own validator accepts:
// a length-prefixed algorithm name followed by its parameters, base64 and
// nothing else. A placeholder like "AAAA..." would round-trip through this
// client perfectly and be refused by the API, which is the wrong thing for an
// example to teach.
const (
	ed25519Blob  = "AAAAC3NzaC1lZDI1NTE5AAAAIFNluHur5rQdQnsTP1TO9Ufn1yB6H/Ax9J25aVber+U6"
	nistp256Blob = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPCxa530CiYpvxDx" +
		"cvhEGWb0kWZjjkGu9M4nexti6vKt9JpjDKk8gNIRRTf6Lg4hj7FhqBBbKR8GXr0P5CHU/Hg="
)

// A key with no marker: `marker` is JSON null, which is how the server spells an
// ordinary entry.
var hostKeyJSON = fmt.Sprintf(`{"data":{"id":"invhkey-9999","type":"inventory-host-keys",
  "attributes":{"key-type":"ssh-ed25519","public-key":%q,"marker":null,
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"},
  "relationships":{"host":{"data":{"id":"invhost-1111","type":"inventory-hosts"}}}}}`,
	ed25519Blob)

// The same key, revoked -- the outgoing half of a rotation.
var revokedHostKeyJSON = fmt.Sprintf(`{"data":{"id":"invhkey-9999",
  "type":"inventory-host-keys","attributes":{"key-type":"ssh-ed25519",
  "public-key":%q,"marker":"revoked",
  "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-02T00:00:00Z"},
  "relationships":{"host":{"data":{"id":"invhost-1111","type":"inventory-hosts"}}}}}`,
	ed25519Blob)

// ── Create ───────────────────────────────────────────────────────────────────

func TestCreateInventoryHostKey(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, hostKeyJSON)

	key, err := c.CreateInventoryHostKey(t.Context(), "invhost-1111",
		CreateInventoryHostKeyRequest{KeyType: "ssh-ed25519", PublicKey: ed25519Blob})
	if err != nil {
		t.Fatal(err)
	}
	// The host comes from the PATH, as it does for a host variable.
	wantReq(t, got, http.MethodPost, "/api/v1/inventory-hosts/invhost-1111/keys")
	if got.resourceType != "inventory-host-keys" {
		t.Errorf("resource type: %q", got.resourceType)
	}
	if got.attrs["key-type"] != "ssh-ed25519" || got.attrs["public-key"] != ed25519Blob {
		t.Errorf("identity not sent: %v", got.attrs)
	}
	// An absent marker is OMITTED rather than sent as "": the server reads both
	// as "no marker", so putting the empty string on the wire would be sending a
	// value that means the absence of one.
	if _, ok := got.attrs["marker"]; ok {
		t.Errorf("an empty marker must not be sent: %v", got.attrs)
	}

	if key.ID != "invhkey-9999" || key.KeyType != "ssh-ed25519" {
		t.Errorf("response not parsed: %+v", key)
	}
	// The blob comes back IN FULL -- unlike a sensitive variable's value, which
	// reads back as MaskedValue. A host's public key is public, and masking it
	// would hide the one value an operator needs at the moment a host presents
	// something else.
	if key.PublicKey != ed25519Blob {
		t.Errorf("the public key must come back in full, got %q", key.PublicKey)
	}
	// JSON null reads as "", which is the same thing: the server normalises null
	// and "" to the same stored value, so there is no third state to carry.
	if key.Marker != "" {
		t.Errorf("a null marker must read as empty, got %q", key.Marker)
	}
	if key.HostID != "invhost-1111" {
		t.Errorf("host relationship: %q", key.HostID)
	}
}

func TestCreateInventoryHostKeySendsAMarker(t *testing.T) {
	c, got := inventoryServer(t, http.StatusCreated, revokedHostKeyJSON)

	key, err := c.CreateInventoryHostKey(t.Context(), "invhost-1111",
		CreateInventoryHostKeyRequest{
			KeyType: "ssh-ed25519", PublicKey: ed25519Blob, Marker: "revoked",
		})
	if err != nil {
		t.Fatal(err)
	}
	// Without the `@` -- Terrapod adds that when it renders the line, and the
	// server refuses a marker that carries one.
	if got.attrs["marker"] != "revoked" {
		t.Errorf("marker not sent: %v", got.attrs)
	}
	if key.Marker != "revoked" {
		t.Errorf("marker not parsed: %+v", key)
	}
}

// Declaring the same key twice under the same algorithm is a conflict. Two keys
// of ONE algorithm are allowed -- that is the mid-rotation state the row's
// identity exists to be able to hold -- but the same key twice is not.
func TestCreateInventoryHostKeyDuplicateIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusConflict,
		`{"errors":[{"detail":"This host already has that exact key declared for that algorithm.","status":"409"}],
		  "detail":"This host already has that exact key declared for that algorithm."}`)

	key, err := c.CreateInventoryHostKey(t.Context(), "invhost-1111",
		CreateInventoryHostKeyRequest{KeyType: "ssh-ed25519", PublicKey: ed25519Blob})
	if err == nil {
		t.Fatal("a duplicate declaration must be an error")
	}
	if key != nil {
		t.Errorf("want no key alongside the error, got %+v", key)
	}
}

// A blob the server refuses -- a whole known_hosts line, an unrecognised
// algorithm, a type that disagrees with the blob's own embedded name -- is a
// 422, and must not come back as a key.
func TestCreateInventoryHostKeyValidationIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusUnprocessableEntity,
		`{"errors":[{"detail":"a host public key must be the base64 blob ALONE","status":"422"}],
		  "detail":"a host public key must be the base64 blob ALONE"}`)

	key, err := c.CreateInventoryHostKey(t.Context(), "invhost-1111",
		CreateInventoryHostKeyRequest{
			KeyType: "ssh-ed25519", PublicKey: "web-01 ssh-ed25519 " + ed25519Blob,
		})
	if err == nil {
		t.Fatal("a refused public key must be an error")
	}
	if key != nil {
		t.Errorf("want no key alongside the error, got %+v", key)
	}
}

// ── List a host's keys ───────────────────────────────────────────────────────

func TestListInventoryHostKeys(t *testing.T) {
	// TWO keys of one algorithm, which is the shape this table exists to hold:
	// the incoming key and the revoked outgoing one, mid-rotation.
	body := fmt.Sprintf(`{"data":[
	  {"id":"invhkey-a","type":"inventory-host-keys","attributes":{
	    "key-type":"ssh-ed25519","public-key":%q,"marker":null}},
	  {"id":"invhkey-b","type":"inventory-host-keys","attributes":{
	    "key-type":"ssh-ed25519","public-key":%q,"marker":"revoked"}},
	  {"id":"invhkey-c","type":"inventory-host-keys","attributes":{
	    "key-type":"ecdsa-sha2-nistp256","public-key":%q,"marker":null}}],
	  "meta":{"pagination":{"current-page":1,"page-size":20,"total-count":3,"total-pages":1}}}`,
		ed25519Blob, ed25519Blob, nistp256Blob)
	c, got := inventoryServer(t, http.StatusOK, body)

	keys, err := c.ListInventoryHostKeys(t.Context(), "invhost-1111")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/inventory-hosts/invhost-1111/keys")
	if len(keys) != 3 {
		t.Fatalf("want three keys, got %d: %+v", len(keys), keys)
	}
	// All of them, not one per algorithm family: a client picks by its own
	// HostKeyAlgorithms preference, so a trust set that kept one per family
	// would refuse the host whenever the two disagreed.
	if keys[0].KeyType != keys[1].KeyType {
		t.Errorf("two keys of one algorithm must both survive: %+v", keys)
	}
	if keys[0].Marker != "" || keys[1].Marker != "revoked" {
		t.Errorf("markers are per key, not per algorithm: %+v", keys)
	}
	if keys[2].KeyType != "ecdsa-sha2-nistp256" || keys[2].PublicKey != nistp256Blob {
		t.Errorf("third key not parsed: %+v", keys[2])
	}
}

func TestListInventoryHostKeysForbiddenIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusForbidden,
		`{"errors":[{"detail":"inventory:read required","status":"403"}],
		  "detail":"inventory:read required"}`)

	keys, err := c.ListInventoryHostKeys(t.Context(), "invhost-1111")
	if err == nil {
		t.Fatal("a refused read must not return an empty trust set")
	}
	if keys != nil {
		t.Errorf("want no keys alongside the error, got %+v", keys)
	}
}

// ── Get one key ──────────────────────────────────────────────────────────────

func TestGetInventoryHostKey(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, revokedHostKeyJSON)

	key, err := c.GetInventoryHostKey(t.Context(), "invhkey-9999")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/inventory-host-keys/invhkey-9999")
	if key.Marker != "revoked" || key.UpdatedAt != "2026-01-02T00:00:00Z" {
		t.Errorf("response not parsed: %+v", key)
	}
}

func TestGetInventoryHostKeyNotFoundIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusNotFound,
		`{"errors":[{"detail":"Not found","status":"404"}],"detail":"Not found"}`)

	key, err := c.GetInventoryHostKey(t.Context(), "invhkey-gone")
	if err == nil {
		t.Fatal("a missing key must be an error")
	}
	if key != nil {
		t.Errorf("want no key alongside the error, got %+v", key)
	}
}

// ── Update: the marker, and only the marker ──────────────────────────────────

func TestUpdateInventoryHostKeySendsOnlyTheMarker(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, revokedHostKeyJSON)

	key, err := c.UpdateInventoryHostKey(t.Context(), "invhkey-9999",
		UpdateInventoryHostKeyRequest{Marker: invStr("revoked")})
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodPatch, "/api/v1/inventory-host-keys/invhkey-9999")
	if got.attrs["marker"] != "revoked" {
		t.Errorf("marker not sent: %v", got.attrs)
	}
	// The server REFUSES either of these with 422, because they are the row's
	// identity -- so a request type that could carry them would be a request
	// type that can only fail. This asserts the body, not just the struct.
	for _, frozen := range []string{"key-type", "public-key"} {
		if _, ok := got.attrs[frozen]; ok {
			t.Errorf("%s is part of the key's identity and must never be PATCHed: %v",
				frozen, got.attrs)
		}
	}
	if key.Marker != "revoked" {
		t.Errorf("response not parsed: %+v", key)
	}
}

// An absent marker leaves it alone. The server distinguishes "supplied" from
// "absent", so an unconditional send would clear a marker a caller never
// mentioned -- #316's shape: apply only what the caller sent.
func TestUpdateInventoryHostKeyOmitsAnAbsentMarker(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, hostKeyJSON)

	if _, err := c.UpdateInventoryHostKey(t.Context(), "invhkey-9999",
		UpdateInventoryHostKeyRequest{}); err != nil {
		t.Fatal(err)
	}
	if _, ok := got.attrs["marker"]; ok {
		t.Errorf("an unset Marker must send no attribute at all: %v", got.attrs)
	}
}

// A pointer to "" is how a marker is CLEARED, so it must reach the wire rather
// than collapsing into the absent case above -- the two mean opposite things.
func TestUpdateInventoryHostKeyClearsWithAnEmptyMarker(t *testing.T) {
	c, got := inventoryServer(t, http.StatusOK, hostKeyJSON)

	if _, err := c.UpdateInventoryHostKey(t.Context(), "invhkey-9999",
		UpdateInventoryHostKeyRequest{Marker: invStr("")}); err != nil {
		t.Fatal(err)
	}
	raw, ok := got.attrs["marker"]
	if !ok {
		t.Fatalf("clearing a marker must send it: %v", got.attrs)
	}
	if raw != "" {
		t.Errorf("marker = %v, want the empty string", raw)
	}
}

func TestUpdateInventoryHostKeyRefusalIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusUnprocessableEntity,
		`{"errors":[{"detail":"host key marker 'retired' is not one OpenSSH defines","status":"422"}],
		  "detail":"host key marker 'retired' is not one OpenSSH defines"}`)

	key, err := c.UpdateInventoryHostKey(t.Context(), "invhkey-9999",
		UpdateInventoryHostKeyRequest{Marker: invStr("retired")})
	if err == nil {
		t.Fatal("an unknown marker must be an error")
	}
	if key != nil {
		t.Errorf("want no key alongside the error, got %+v", key)
	}
}

// ── Delete ───────────────────────────────────────────────────────────────────

func TestDeleteInventoryHostKey(t *testing.T) {
	c, got := inventoryServer(t, http.StatusNoContent, "")

	if err := c.DeleteInventoryHostKey(t.Context(), "invhkey-9999"); err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodDelete, "/api/v1/inventory-host-keys/invhkey-9999")
}

func TestDeleteInventoryHostKeyNotFoundIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusNotFound,
		`{"errors":[{"detail":"Not found","status":"404"}],"detail":"Not found"}`)

	if err := c.DeleteInventoryHostKey(t.Context(), "invhkey-gone"); err == nil {
		t.Fatal("deleting a missing key must be an error, not a silent success")
	}
}

// ── The workspace-wide overlay ───────────────────────────────────────────────

func TestListInventoryHostKeysByHost(t *testing.T) {
	// Note the shape: `keys` is a list of ATTRIBUTE objects, with no id and no
	// relationships. That is the route's contract, which is why this overlay has
	// its own type rather than reusing the addressable row.
	body := fmt.Sprintf(`{"data":[
	  {"id":"switch1","type":"inventory-host-keys-by-host","attributes":{"keys":[
	    {"key-type":"ssh-rsa","public-key":%q,"marker":"cert-authority",
	     "created-at":"2026-01-01T00:00:00Z","updated-at":"2026-01-01T00:00:00Z"}]}},
	  {"id":"web-01","type":"inventory-host-keys-by-host","attributes":{"keys":[
	    {"key-type":"ecdsa-sha2-nistp256","public-key":%q,"marker":null},
	    {"key-type":"ssh-ed25519","public-key":%q,"marker":null}]}}],
	  "meta":{"pagination":{"current-page":1,"page-size":20,"total-count":2,"total-pages":1}}}`,
		ed25519Blob, nistp256Blob, ed25519Blob)
	c, got := inventoryServer(t, http.StatusOK, body)

	hosts, err := c.ListInventoryHostKeysByHost(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	wantReq(t, got, http.MethodGet, "/api/v1/workspaces/ws-abc/inventory/host-keys")
	if len(hosts) != 2 {
		t.Fatalf("want two hosts, got %d: %+v", len(hosts), hosts)
	}
	// Keyed on the host NAME, because that is what the resolved document beside
	// it is keyed on and the two are meant to be read together.
	if hosts[0].HostName != "switch1" || hosts[1].HostName != "web-01" {
		t.Errorf("host names not read off the resource id: %+v", hosts)
	}
	if len(hosts[0].Keys) != 1 || hosts[0].Keys[0].Marker != "cert-authority" {
		t.Errorf("switch1's key not parsed: %+v", hosts[0])
	}
	if len(hosts[1].Keys) != 2 {
		t.Fatalf("want both of web-01's keys, got %+v", hosts[1].Keys)
	}
	// Every attribute, not just the two that identify the line -- the renderer
	// reads the marker and an operator reads the timestamps.
	if hosts[1].Keys[1].KeyType != "ssh-ed25519" ||
		hosts[1].Keys[1].PublicKey != ed25519Blob {
		t.Errorf("web-01's second key not parsed: %+v", hosts[1].Keys[1])
	}
	if hosts[0].Keys[0].CreatedAt != "2026-01-01T00:00:00Z" {
		t.Errorf("timestamps not parsed: %+v", hosts[0].Keys[0])
	}
}

// A host with an empty key list stays in the answer as a host with no keys,
// rather than being dropped -- the same reason ResolvedInventory enumerates a
// var-less host.
func TestListInventoryHostKeysByHostKeepsAHostWithNoKeys(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusOK,
		`{"data":[{"id":"web-01","type":"inventory-host-keys-by-host",
		  "attributes":{"keys":[]}}],
		  "meta":{"pagination":{"current-page":1,"page-size":20,
		    "total-count":1,"total-pages":1}}}`)

	hosts, err := c.ListInventoryHostKeysByHost(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(hosts) != 1 || hosts[0].HostName != "web-01" {
		t.Fatalf("the host must survive an empty key list: %+v", hosts)
	}
	if len(hosts[0].Keys) != 0 {
		t.Errorf("want no keys, got %+v", hosts[0].Keys)
	}
}

func TestListInventoryHostKeysByHostFailureIsAnError(t *testing.T) {
	c, _ := inventoryServer(t, http.StatusForbidden,
		`{"errors":[{"detail":"inventory:read required","status":"403"}],
		  "detail":"inventory:read required"}`)

	hosts, err := c.ListInventoryHostKeysByHost(t.Context(), "ws-abc")
	if err == nil {
		t.Fatal("a refused read must not return an empty overlay")
	}
	if hosts != nil {
		t.Errorf("want no hosts alongside the error, got %+v", hosts)
	}
}

// The overlay has one entry per HOST, so it is routinely larger than one page --
// which is why this collection has a ListAll and a host's own key set does not.
func TestListAllInventoryHostKeysByHostPages(t *testing.T) {
	var pages []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page[number]")
		pages = append(pages, page)
		name := "web-01"
		if page == "2" {
			name = "web-02"
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		_, _ = fmt.Fprintf(w, `{"data":[{"id":%q,"type":"inventory-host-keys-by-host",
		  "attributes":{"keys":[{"key-type":"ssh-ed25519","public-key":%q}]}}],
		  "meta":{"pagination":{"current-page":%s,"page-size":100,
		    "total-count":2,"total-pages":2}}}`, name, ed25519Blob, page)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	hosts, err := c.ListAllInventoryHostKeysByHost(t.Context(), "ws-abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(pages) != 2 || pages[0] != "1" || pages[1] != "2" {
		t.Errorf("want two pages requested in order, got %v", pages)
	}
	if len(hosts) != 2 || hosts[0].HostName != "web-01" || hosts[1].HostName != "web-02" {
		t.Errorf("want both pages' hosts, got %+v", hosts)
	}
}
