package mcpserver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// The artifact-ledger read tool (#2589) driven end to end: a real MCP client
// against a real server, backed by a fake Terrapod. The catalogue golden pins
// registration and the schema; these pin what the tool asks the API for and
// what it surfaces -- including the field whose absence would let an agent read
// "you did not ask for it" as "there is nothing".

func ledgerToolCaller(t *testing.T, handler http.HandlerFunc) *mcp.ClientSession {
	t.Helper()
	api := httptest.NewServer(handler)
	t.Cleanup(api.Close)

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: api.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	srv := mcp.NewServer(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	registerArtifactLedger(srv, c)

	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := srv.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).
		Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { _ = sess.Close() })
	return sess
}

const ledgerEntryBody = `{"data":{"id":"sha256:abc123","type":"artifact-ledger-entries","attributes":{
  "digest":"sha256:abc123","kind":"package",
  "coordinates":[{"kind":"pypi","name":"flask","version":"3.0.0"}],
  "attestation":"upstream-publisher",
  "attestor":{"publisher":"pallets/flask"},
  "verification":"verified",
  "admitted-at":"2026-11-01T00:00:00Z","admission":{"policy":"default"},
  "supply-chain":{"state":"findings","reason":null,"findings":3,"critical":1,"malicious":2,
    "verdict":{"matcher":"trivy"}},
  "packages":32,
  "sbom":{"bomFormat":"CycloneDX","components":[{"name":"werkzeug"}]},
  "matches":[{"id":"CVE-2026-0001","severity":"CRITICAL"}],
  "verdict-provenance":{"db-epoch":"2026-11-19"},
  "updated-at":"2026-11-19T00:00:00Z"}}}`

// The tool is READ-ONLY, and the annotation is what a host keys its
// confirmation prompt off -- so it is the authoritative statement, not the
// description's closing sentence.
func TestArtifactLedgerToolIsReadOnly(t *testing.T) {
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(ledgerEntryBody))
	})
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	if len(res.Tools) != 1 || res.Tools[0].Name != "terrapod_artifact_ledger_get" {
		t.Fatalf("registered tools: %+v", res.Tools)
	}
	ann := res.Tools[0].Annotations
	if ann == nil || !ann.ReadOnlyHint {
		t.Errorf("the ledger read must be annotated read-only: %+v", ann)
	}
	if ann != nil && ann.DestructiveHint != nil && *ann.DestructiveHint {
		t.Error("a read must not be annotated destructive")
	}
}

func TestArtifactLedgerGetTool(t *testing.T) {
	var method, target string
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, r *http.Request) {
		method, target = r.Method, r.RequestURI
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(ledgerEntryBody))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_artifact_ledger_get",
		Arguments: map[string]any{"digest": "sha256:abc123"},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	// RequestURI rather than URL.Path: the latter is already decoded, so it
	// cannot see the digest's colon being percent-encoded on the way out --
	// which would address a digest the server does not hold.
	if method != http.MethodGet || target != "/api/v1/admin/artifact-ledger/sha256:abc123" {
		t.Errorf("reached %s %s", method, target)
	}

	out := resultText(t, res)
	for _, want := range []string{
		`"digest":"sha256:abc123"`, `"kind":"package"`,
		// Provenance and the result of checking it are separate answers and
		// both have to survive: the first says what was claimed, the second
		// what came of it.
		`"attestation":"upstream-publisher"`, `"verification":"verified"`,
		`"publisher":"pallets/flask"`,
		// The state must travel beside the counts, or a zero cannot be read
		// honestly.
		`"state":"findings"`, `"findings":3`, `"critical":1`,
		// Malicious is surfaced apart from findings because the two warrant
		// different responses.
		`"malicious":2`,
		`"packages":32`,
		`"CVE-2026-0001"`,
		// Without provenance, a verdict is undated and unattributable.
		`"db-epoch":"2026-11-19"`,
		`"admitted_at":"2026-11-01T00:00:00Z"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
}

// The SBOM is withheld by default because it carries a component per
// dependency. That is only safe if the result says so: an agent that cannot
// tell "withheld" from "none exists" would read a present inventory as a
// missing one.
func TestArtifactLedgerGetToolWithholdsTheSBOMButSaysSo(t *testing.T) {
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(ledgerEntryBody))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_artifact_ledger_get",
		Arguments: map[string]any{"digest": "sha256:abc123"},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	out := resultText(t, res)
	if strings.Contains(out, "werkzeug") {
		t.Errorf("the SBOM must be withheld unless asked for:\n%s", out)
	}
	if !strings.Contains(out, `"sbom_omitted":true`) {
		t.Errorf("a withheld SBOM must be declared, or its absence reads as none existing:\n%s", out)
	}
}

func TestArtifactLedgerGetToolReturnsTheSBOMWhenAsked(t *testing.T) {
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(ledgerEntryBody))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_artifact_ledger_get",
		Arguments: map[string]any{"digest": "sha256:abc123", "include_sbom": true},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	out := resultText(t, res)
	if !strings.Contains(out, "werkzeug") {
		t.Errorf("include_sbom must return the document:\n%s", out)
	}
	// And it must NOT also claim to have withheld it.
	if strings.Contains(out, "sbom_omitted") {
		t.Errorf("a returned SBOM must not be reported as omitted:\n%s", out)
	}
}

// An entry with no inventory at all must not report sbom_omitted either: there
// is nothing being withheld, and saying otherwise sends an agent back for a
// document that does not exist.
func TestArtifactLedgerGetToolDistinguishesNoSBOMFromAWithheldOne(t *testing.T) {
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"sha256:def","type":"artifact-ledger-entries","attributes":{
		  "digest":"sha256:def","kind":"binary","coordinates":[],
		  "attestation":null,"attestor":{},"verification":"unverified",
		  "admitted-at":"2026-11-01T00:00:00Z","admission":{},
		  "supply-chain":{"state":"not-inventoried","reason":"no cataloger for this artifact shape",
		    "findings":0,"critical":0,"malicious":0,"verdict":null},
		  "packages":0,"sbom":null,"matches":[],"verdict-provenance":{},
		  "updated-at":"2026-11-01T00:00:00Z"}}}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_artifact_ledger_get",
		Arguments: map[string]any{"digest": "sha256:def"},
	})
	if err != nil || res.IsError {
		t.Fatalf("call: %v %s", err, resultText(t, res))
	}
	out := resultText(t, res)
	if strings.Contains(out, "sbom_omitted") {
		t.Errorf("nothing was withheld, so nothing may claim to have been:\n%s", out)
	}
	// The state and its reason are what make the three zeros readable as
	// "nothing looked" rather than "nothing found".
	for _, want := range []string{`"state":"not-inventoried"`, `"no cataloger for this artifact shape"`} {
		if !strings.Contains(out, want) {
			t.Errorf("result missing %s:\n%s", want, out)
		}
	}
}

// A digest with no entry is a meaningful answer rather than an empty one, so
// the tool must surface the server's refusal rather than an empty success.
func TestArtifactLedgerGetToolSurfacesANotFound(t *testing.T) {
	sess := ledgerToolCaller(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404","detail":"no ledger entry for that digest"}]}`))
	})
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "terrapod_artifact_ledger_get",
		Arguments: map[string]any{"digest": "sha256:missing"},
	})
	if err != nil {
		t.Fatalf("call: %v", err)
	}
	if !res.IsError {
		t.Fatalf("a missing entry must be an error result, not an empty success:\n%s", resultText(t, res))
	}
	// Both halves: the digest asked for, so the agent can tell WHICH lookup
	// came back empty, and the server's own sentence rather than a generic
	// "resource not found" that identifies nothing.
	out := resultText(t, res)
	for _, want := range []string{"sha256:missing", "artifact-ledger-entry"} {
		if !strings.Contains(out, want) {
			t.Errorf("the refusal must name %s:\n%s", want, out)
		}
	}
}
