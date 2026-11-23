package mcpserver

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The approved-SSH-host-key tools (#2592) driven end to end: a real MCP client
// against a real server, backed by a fake Terrapod. The catalogue golden pins
// registration and the input schemas, and TestEveryToolIsAnnotated pins the
// annotations; these pin that each tool reaches the right endpoint and
// surfaces what an agent has to reason over.

// Real blobs, because the API checks a blob's own embedded algorithm name
// against the declared type -- so a placeholder would pass through these tools
// and be refused by the server.
const (
	hkEd  = "AAAAC3NzaC1lZDI1NTE5AAAAIFNluHur5rQdQnsTP1TO9Ufn1yB6H/Ax9J25aVber+U6"
	hkEd2 = "AAAAC3NzaC1lZDI1NTE5AAAAIGnOPgMYxyAPzBg9kbPsBhFoTsDRTY1eDiRYNDpQsVN3"
)

// ── The workspace-wide view ──────────────────────────────────────────────────

func TestInventoryHostKeysIsKeyedOnHostName(t *testing.T) {
	// Note the shape: `keys` is a list of ATTRIBUTE objects with no id, and the
	// resource id is the host NAME -- the same key the resolved document uses,
	// which is what lets the two be read side by side.
	sess, seen := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-abc/inventory/host-keys": `{"data":[
		  {"id":"switch1","type":"inventory-host-keys-by-host","attributes":{"keys":[
		    {"key-type":"ssh-rsa","public-key":"AAAAB3NzaC1yc2EAAAA","marker":"cert-authority"}]}},
		  {"id":"web-01","type":"inventory-host-keys-by-host","attributes":{"keys":[
		    {"key-type":"ssh-ed25519","public-key":"` + hkEd + `","marker":null},
		    {"key-type":"ssh-ed25519","public-key":"` + hkEd2 + `","marker":"revoked"}]}},
		  {"id":"quiet","type":"inventory-host-keys-by-host","attributes":{"keys":[]}}],
		  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":3,"total-pages":1}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_keys",
		map[string]any{"workspace_id": "ws-abc"})

	var out struct {
		Hosts map[string][]struct {
			KeyType   string `json:"key-type"`
			PublicKey string `json:"public-key"`
			Marker    string `json:"marker"`
		} `json:"hosts"`
	}
	invStructured(t, res, &out)

	if len(out.Hosts) != 3 {
		t.Fatalf("want three hosts, got %+v", out.Hosts)
	}
	// Two keys of ONE algorithm, told apart by their blob -- the mid-rotation
	// state. An agent must not read the second as a duplicate.
	web := out.Hosts["web-01"]
	if len(web) != 2 {
		t.Fatalf("web-01 must carry both keys: %+v", web)
	}
	if web[0].KeyType != web[1].KeyType || web[0].PublicKey == web[1].PublicKey {
		t.Errorf("both keys are ssh-ed25519 and differ by blob: %+v", web)
	}
	// The marker is per key, not per algorithm.
	if web[0].Marker != "" || web[1].Marker != "revoked" {
		t.Errorf("markers = %q, %q", web[0].Marker, web[1].Marker)
	}
	// The blob is NOT masked: a host's public key is public, and hiding it
	// would hide the one value needed when a host presents something else.
	if web[0].PublicKey != hkEd {
		t.Errorf("the public key must come back in full, got %q", web[0].PublicKey)
	}
	if out.Hosts["switch1"][0].Marker != "cert-authority" {
		t.Errorf("switch1 = %+v", out.Hosts["switch1"])
	}
	// A declared host approved to present nothing stays in the answer as an
	// EMPTY LIST rather than being dropped -- "no approved key" is a real and
	// important answer on a workspace with host-key checking on.
	keys, present := out.Hosts["quiet"]
	if !present || len(keys) != 0 {
		t.Errorf("a host with no keys must survive as an empty list: %+v", out.Hosts)
	}

	var reached bool
	for _, p := range seen.paths {
		if p == "/api/v1/workspaces/ws-abc/inventory/host-keys" {
			reached = true
		}
	}
	if !reached {
		t.Errorf("the workspace-wide route was not reached: %v", seen.paths)
	}
}

// A workspace with no approved key anywhere answers with an EMPTY MAP, not
// null: `null` fails the tool's derived output schema and costs the agent the
// whole result.
func TestInventoryHostKeysAnswersAnEmptyMap(t *testing.T) {
	sess, _ := inventoryRouter(t, map[string]string{
		"/api/v1/workspaces/ws-abc/inventory/host-keys": `{"data":[],
		  "meta":{"pagination":{"current-page":1,"page-size":100,"total-count":0,"total-pages":1}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_keys",
		map[string]any{"workspace_id": "ws-abc"})

	raw, err := json.Marshal(res.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), `"hosts":null`) {
		t.Errorf("hosts must be an empty object, not null: %s", raw)
	}
	var out struct {
		Hosts map[string]any `json:"hosts"`
	}
	invStructured(t, res, &out)
	if out.Hosts == nil || len(out.Hosts) != 0 {
		t.Errorf("hosts = %+v, want an empty map", out.Hosts)
	}
}

func TestInventoryHostKeysRequiresTheWorkspace(t *testing.T) {
	sess, _ := inventoryRouter(t, map[string]string{})
	res := callInventoryTool(t, sess, "terrapod_inventory_host_keys", map[string]any{})
	if !res.IsError {
		t.Error("a missing workspace_id must be refused rather than read as a workspace")
	}
}

// A failed read is an ERROR, never an empty trust set: reading "this host is
// approved to present nothing" when the call actually failed is the worst
// possible answer for an agent about to declare a key.
func TestInventoryHostKeysFailureIsAnError(t *testing.T) {
	sess, _ := inventoryRouter(t, map[string]string{})
	res := callInventoryTool(t, sess, "terrapod_inventory_host_keys",
		map[string]any{"workspace_id": "ws-abc"})
	if !res.IsError {
		t.Error("a refused read must be an error, not an empty answer")
	}
}

// ── Declare ──────────────────────────────────────────────────────────────────

func TestInventoryHostKeyDeclareCreates(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"POST /api/v1/inventory-hosts/invhost-1/keys": `{"data":{"id":"invhkey-1",
		  "type":"inventory-host-keys","attributes":{"key-type":"ssh-ed25519",
		  "public-key":"` + hkEd + `","marker":null},
		  "relationships":{"host":{"data":{"id":"invhost-1","type":"inventory-hosts"}}}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_declare", map[string]any{
		"host_id": "invhost-1", "key_type": "ssh-ed25519", "public_key": hkEd,
	})

	var key terrapod.InventoryHostKey
	invStructured(t, res, &key)
	if key.ID != "invhkey-1" || key.KeyType != "ssh-ed25519" {
		t.Errorf("key not surfaced: %+v", key)
	}

	if len(seen.paths) != 1 || seen.paths[0] != "/api/v1/inventory-hosts/invhost-1/keys" {
		t.Fatalf("the host comes from the path: %v", seen.paths)
	}
	if seen.methods[0] != "POST" {
		t.Errorf("method = %s", seen.methods[0])
	}
	var sent struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(seen.bodies[0]), &sent); err != nil {
		t.Fatalf("body is not JSON:API: %s", seen.bodies[0])
	}
	if sent.Data.Attributes["key-type"] != "ssh-ed25519" ||
		sent.Data.Attributes["public-key"] != hkEd {
		t.Errorf("identity not sent: %+v", sent.Data.Attributes)
	}
	// An omitted marker sends no attribute: the server reads both an absent
	// field and "" as "no marker", so sending "" would put a value on the wire
	// that means the absence of one.
	if _, ok := sent.Data.Attributes["marker"]; ok {
		t.Errorf("an omitted marker must not be sent: %+v", sent.Data.Attributes)
	}
}

// "Declare" has to mean declare. A key already present is a 409, and an agent
// asked to revoke a key by VALUE should not have to know whether it is there --
// so the conflict is recovered by finding the row and setting its marker.
func TestInventoryHostKeyDeclareIsIdempotentAndSetsTheMarker(t *testing.T) {
	// The POST conflicts; everything else is served from the map. Both of the
	// host's keys are ssh-ed25519 and differ only by blob -- the mid-rotation
	// state -- so this also pins that the right one is found.
	sess, seen := conflictOnPost(t, map[string]string{
		"GET /api/v1/inventory-hosts/invhost-1/keys": `{"data":[
		  {"id":"invhkey-old","type":"inventory-host-keys","attributes":{
		    "key-type":"ssh-ed25519","public-key":"` + hkEd + `","marker":null}},
		  {"id":"invhkey-new","type":"inventory-host-keys","attributes":{
		    "key-type":"ssh-ed25519","public-key":"` + hkEd2 + `","marker":null}}]}`,
		"PATCH /api/v1/inventory-host-keys/invhkey-old": `{"data":{"id":"invhkey-old",
		  "type":"inventory-host-keys","attributes":{"key-type":"ssh-ed25519",
		  "public-key":"` + hkEd + `","marker":"revoked"}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_declare", map[string]any{
		"host_id": "invhost-1", "key_type": "ssh-ed25519",
		"public_key": hkEd, "marker": "revoked",
	})

	var key terrapod.InventoryHostKey
	invStructured(t, res, &key)
	if key.Marker != "revoked" {
		t.Errorf("the marker must be set on the existing row: %+v", key)
	}
	// The OLD key's row, not the new one. Both are ssh-ed25519, so matching on
	// the algorithm alone would have revoked the incoming key -- which is the
	// opposite of a rotation.
	var patched string
	for i, p := range seen.paths {
		if seen.methods[i] == "PATCH" {
			patched = p
		}
	}
	if patched != "/api/v1/inventory-host-keys/invhkey-old" {
		t.Errorf("patched %q, want the row whose BLOB matches", patched)
	}
}

// A conflict that is not this key must report the SERVER's error rather than an
// explanation this tool invented.
func TestInventoryHostKeyDeclareReportsAnUnexplainedConflict(t *testing.T) {
	sess, seen := conflictOnPost(t, map[string]string{
		// The host holds a different key entirely, so the conflict is not
		// explained by anything this tool can patch.
		"GET /api/v1/inventory-hosts/invhost-1/keys": `{"data":[
		  {"id":"invhkey-other","type":"inventory-host-keys","attributes":{
		    "key-type":"ssh-rsa","public-key":"AAAAB3NzaC1yc2EAAAA","marker":null}}]}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_declare", map[string]any{
		"host_id": "invhost-1", "key_type": "ssh-ed25519", "public_key": hkEd,
	})
	if !res.IsError {
		t.Fatal("an unexplained conflict must be reported")
	}
	for i, p := range seen.paths {
		if seen.methods[i] == "PATCH" {
			t.Errorf("nothing may be patched when the conflict is not this key: %s", p)
		}
	}
}

func TestInventoryHostKeyDeclareRefusesAMissingArgument(t *testing.T) {
	for _, c := range []struct {
		name string
		args map[string]any
	}{
		{"no host", map[string]any{"key_type": "ssh-ed25519", "public_key": hkEd}},
		{"no key type", map[string]any{"host_id": "invhost-1", "public_key": hkEd}},
		{"no public key", map[string]any{"host_id": "invhost-1", "key_type": "ssh-ed25519"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			sess, seen := inventoryRouter(t, map[string]string{})
			res := callInventoryTool(t, sess, "terrapod_inventory_host_key_declare", c.args)
			if !res.IsError {
				t.Error("a missing argument must be refused before any call")
			}
			if len(seen.paths) != 0 {
				t.Errorf("nothing should have been called: %v", seen.paths)
			}
		})
	}
}

// ── Mark ─────────────────────────────────────────────────────────────────────

func TestInventoryHostKeyMarkSetsTheMarker(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"PATCH /api/v1/inventory-host-keys/invhkey-1": `{"data":{"id":"invhkey-1",
		  "type":"inventory-host-keys","attributes":{"key-type":"ssh-ed25519",
		  "public-key":"` + hkEd + `","marker":"revoked"}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_mark",
		map[string]any{"key_id": "invhkey-1", "marker": "revoked"})

	var key terrapod.InventoryHostKey
	invStructured(t, res, &key)
	if key.Marker != "revoked" {
		t.Errorf("marker not surfaced: %+v", key)
	}
	if seen.methods[0] != "PATCH" || seen.paths[0] != "/api/v1/inventory-host-keys/invhkey-1" {
		t.Fatalf("wrong request: %s %s", seen.methods[0], seen.paths[0])
	}

	var sent struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(seen.bodies[0]), &sent); err != nil {
		t.Fatalf("body is not JSON:API: %s", seen.bodies[0])
	}
	if sent.Data.Attributes["marker"] != "revoked" {
		t.Errorf("marker not sent: %+v", sent.Data.Attributes)
	}
	// The identity is NOT sent. The server refuses a patch carrying either with
	// 422, so a tool that sent them could only ever fail.
	for _, frozen := range []string{"key-type", "public-key"} {
		if _, ok := sent.Data.Attributes[frozen]; ok {
			t.Errorf("%s is part of the key's identity and must never be patched: %+v",
				frozen, sent.Data.Attributes)
		}
	}
}

// Omitting `marker` CLEARS it. This tool always writes the field -- there is no
// way to call it as a no-op -- so the empty value has to reach the wire, where
// the server reads a supplied empty marker as "remove it".
func TestInventoryHostKeyMarkClearsWhenTheMarkerIsOmitted(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"PATCH /api/v1/inventory-host-keys/invhkey-1": `{"data":{"id":"invhkey-1",
		  "type":"inventory-host-keys","attributes":{"key-type":"ssh-ed25519",
		  "public-key":"` + hkEd + `","marker":null}}}`,
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_mark",
		map[string]any{"key_id": "invhkey-1"})

	var key terrapod.InventoryHostKey
	invStructured(t, res, &key)
	if key.Marker != "" {
		t.Errorf("the marker should be cleared: %+v", key)
	}
	var sent struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(seen.bodies[0]), &sent); err != nil {
		t.Fatalf("body is not JSON:API: %s", seen.bodies[0])
	}
	raw, present := sent.Data.Attributes["marker"]
	if !present {
		t.Fatalf("clearing a marker must SEND it; an omitted field means "+
			"\"leave alone\" to the server: %+v", sent.Data.Attributes)
	}
	if raw != "" {
		t.Errorf("marker = %v, want the empty string", raw)
	}
}

func TestInventoryHostKeyMarkRequiresTheID(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{})
	res := callInventoryTool(t, sess, "terrapod_inventory_host_key_mark", map[string]any{})
	if !res.IsError {
		t.Error("a missing key_id must be refused")
	}
	if len(seen.paths) != 0 {
		t.Errorf("nothing should have been called: %v", seen.paths)
	}
}

// ── Removal goes through the one remove tool ─────────────────────────────────

// One remove tool with a prefix table, so there is no kind to pass and no way
// to delete a different kind than intended. An `invhkey-` id that the table did
// not know would be refused as "not an inventory row", which reads as the id
// being wrong rather than the tool being incomplete.
func TestInventoryRemoveAcceptsAnApprovedHostKey(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"DELETE /api/v1/inventory-host-keys/invhkey-1": "",
	})

	res := callInventoryTool(t, sess, "terrapod_inventory_remove",
		map[string]any{"id": "invhkey-1"})

	var out struct {
		Removed string `json:"removed"`
		ID      string `json:"id"`
	}
	invStructured(t, res, &out)
	if out.Removed != "approved host key" || out.ID != "invhkey-1" {
		t.Errorf("out = %+v", out)
	}
	if seen.methods[0] != "DELETE" ||
		seen.paths[0] != "/api/v1/inventory-host-keys/invhkey-1" {
		t.Errorf("wrong request: %s %s", seen.methods[0], seen.paths[0])
	}
}

// `invhvar-` and `invhkey-` share four characters, so the table has to match the
// whole prefix rather than the first few: a host key removed through the
// host-variable route would 404, and a variable removed through the key route
// would too -- both reading as a missing row.
func TestInventoryRemoveDoesNotConfuseAKeyWithAHostVariable(t *testing.T) {
	sess, seen := inventoryRouter(t, map[string]string{
		"DELETE /api/v1/inventory-host-vars/invhvar-1": "",
	})
	callInventoryTool(t, sess, "terrapod_inventory_remove", map[string]any{"id": "invhvar-1"})
	if seen.paths[0] != "/api/v1/inventory-host-vars/invhvar-1" {
		t.Errorf("a host variable must reach the variable route, got %q", seen.paths[0])
	}
}

// ── findHostKeyID ────────────────────────────────────────────────────────────

// Matched on the algorithm AND the blob, which is the row's identity on the
// server. Matching on the algorithm alone would, mid-rotation, return whichever
// of the two keys came back first.
func TestFindHostKeyIDMatchesOnBothHalvesOfTheIdentity(t *testing.T) {
	keys := []terrapod.InventoryHostKey{
		{ID: "invhkey-old", ApprovedHostKey: terrapod.ApprovedHostKey{
			KeyType: "ssh-ed25519", PublicKey: hkEd}},
		{ID: "invhkey-new", ApprovedHostKey: terrapod.ApprovedHostKey{
			KeyType: "ssh-ed25519", PublicKey: hkEd2}},
	}

	if id, ok := findHostKeyID(keys, "ssh-ed25519", hkEd2); !ok || id != "invhkey-new" {
		t.Errorf("got %q %v, want invhkey-new", id, ok)
	}
	if id, ok := findHostKeyID(keys, "ssh-ed25519", hkEd); !ok || id != "invhkey-old" {
		t.Errorf("got %q %v, want invhkey-old", id, ok)
	}
	// The right algorithm with the wrong blob is a DIFFERENT key, not this one.
	if _, ok := findHostKeyID(keys, "ssh-ed25519", "AAAAsomethingelse"); ok {
		t.Error("a blob that is not declared must not match")
	}
	// And the right blob under the wrong algorithm likewise.
	if _, ok := findHostKeyID(keys, "ssh-rsa", hkEd); ok {
		t.Error("the algorithm has to match too")
	}
}

// ── Helpers ──────────────────────────────────────────────────────────────────

// conflictOnPost is inventoryRouter with one difference: every POST answers 409.
//
// Needed because the shared router serves a body per route and cannot express a
// status, and 409 is the one error `declare` RECOVERS from -- so a fake that
// cannot produce it leaves the whole idempotent path untested.
func conflictOnPost(t *testing.T, routes map[string]string) (*mcp.ClientSession, *invRoute) {
	t.Helper()
	seen := &invRoute{}
	sess := inventoryToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		seen.paths = append(seen.paths, r.URL.Path)
		seen.methods = append(seen.methods, r.Method)
		raw, _ := io.ReadAll(r.Body)
		seen.bodies = append(seen.bodies, string(raw))

		w.Header().Set("Content-Type", "application/vnd.api+json")
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"This host already has that exact ` +
				`key declared for that algorithm.","status":"409"}],` +
				`"detail":"This host already has that exact key declared for that algorithm."}`))
			return
		}
		body, ok := routes[r.Method+" "+r.URL.Path]
		if !ok {
			if body, ok = routes[r.URL.Path]; !ok {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(
					`{"errors":[{"detail":"no fixture for this route","status":"404"}]}`))
				return
			}
		}
		if body == "" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		_, _ = w.Write([]byte(body))
	})
	return sess, seen
}
