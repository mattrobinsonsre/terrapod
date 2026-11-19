package terrapod

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestGetArtifactLedgerEntry(t *testing.T) {
	var gotTarget string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// RequestURI, NOT r.URL.Path: the latter is already percent-DECODED,
		// so it reads identically whether the colon went over the wire as `:`
		// or as `%3A` and cannot see the thing this assertion is about. A
		// mutation swapping PathEscape for QueryEscape passed against the
		// decoded path.
		gotTarget = r.RequestURI
		if r.Method != http.MethodGet {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"sha256:abc123","type":"artifact-ledger-entries","attributes":{
		  "digest":"sha256:abc123",
		  "kind":"package",
		  "coordinates":[
		    {"kind":"pypi","name":"flask","version":"3.0.0"},
		    {"kind":"pypi","name":"Flask","version":"3.0.0"}
		  ],
		  "attestation":"upstream-publisher",
		  "attestor":{"publisher":"pallets/flask","issuer":"https://token.actions.githubusercontent.com"},
		  "verification":"verified",
		  "admitted-at":"2026-11-01T00:00:00Z",
		  "admission":{"policy":"default"},
		  "supply-chain":{"state":"findings","reason":null,"findings":3,"critical":1,"malicious":0,
		    "verdict":{"matcher":"trivy"}},
		  "packages":32,
		  "sbom":{"bomFormat":"CycloneDX","specVersion":"1.7"},
		  "matches":[{"id":"CVE-2026-0001","severity":"CRITICAL"}],
		  "verdict-provenance":{"db-epoch":"2026-11-19","syft":"1.57.2"},
		  "updated-at":"2026-11-19T00:00:00Z"}}}`))
	}))
	t.Cleanup(srv.Close)

	entry, err := newCacheClient(t, srv).GetArtifactLedgerEntry(context.Background(), "sha256:abc123")
	if err != nil {
		t.Fatalf("GetArtifactLedgerEntry: %v", err)
	}

	// The digest is the identity AND the path segment. A colon is legal in a
	// path segment, so it must arrive unescaped -- percent-encoding it would
	// address a digest the server does not hold.
	if gotTarget != "/api/v1/admin/artifact-ledger/sha256:abc123" {
		t.Errorf("request target: %q", gotTarget)
	}

	if entry.Digest != "sha256:abc123" || entry.Kind != "package" {
		t.Errorf("identity: %+v", entry)
	}
	// Several coordinates for one digest is the normal case, not an anomaly:
	// collapsing them would lose a naming the artifact was genuinely served
	// under.
	if len(entry.Coordinates) != 2 {
		t.Fatalf("expected two coordinates, got %d: %+v", len(entry.Coordinates), entry.Coordinates)
	}
	if entry.Coordinates[1]["name"] != "Flask" {
		t.Errorf("second coordinate: %+v", entry.Coordinates[1])
	}

	// Attestation and verification are separate answers and must both survive:
	// the first says what was claimed, the second what came of checking it.
	if entry.Attestation != "upstream-publisher" || entry.Verification != "verified" {
		t.Errorf("attestation/verification: %q / %q", entry.Attestation, entry.Verification)
	}
	if entry.Attestor["publisher"] != "pallets/flask" {
		t.Errorf("attestor: %+v", entry.Attestor)
	}
	if entry.AdmittedAt != "2026-11-01T00:00:00Z" || entry.UpdatedAt != "2026-11-19T00:00:00Z" {
		t.Errorf("timestamps: %+v", entry)
	}
	if entry.Admission["policy"] != "default" {
		t.Errorf("admission: %+v", entry.Admission)
	}

	sc := entry.SupplyChain
	if sc.State != "findings" || sc.Findings != 3 || sc.Critical != 1 || sc.Malicious != 0 {
		t.Errorf("supply chain: %+v", sc)
	}
	// A JSON null must land as the empty string rather than failing the decode:
	// `reason` is null on every state but not-inventoried, which is most rows.
	if sc.Reason != "" {
		t.Errorf("reason from null: %q", sc.Reason)
	}
	if sc.Verdict["matcher"] != "trivy" {
		t.Errorf("verdict: %+v", sc.Verdict)
	}

	// Counts must survive as integers. Hand-walking a map[string]any is how a
	// numeric attribute quietly becomes a float64.
	if entry.Packages != 32 {
		t.Errorf("packages: %d", entry.Packages)
	}
	if entry.SBOM["bomFormat"] != "CycloneDX" {
		t.Errorf("sbom: %+v", entry.SBOM)
	}
	if len(entry.Matches) != 1 || entry.Matches[0]["id"] != "CVE-2026-0001" {
		t.Errorf("matches: %+v", entry.Matches)
	}
	// Provenance is what makes a verdict reproducible rather than asserted, so
	// dropping it would leave every finding undated.
	if entry.VerdictProvenance["db-epoch"] != "2026-11-19" {
		t.Errorf("verdict provenance: %+v", entry.VerdictProvenance)
	}
}

// A not-inventoried entry is the case a caller is most likely to misread: all
// three counts are zero, exactly as they are for a clean artifact, and only the
// state tells them apart.
func TestGetArtifactLedgerEntry_NotInventoried(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"sha256:def","type":"artifact-ledger-entries","attributes":{
		  "digest":"sha256:def","kind":"binary","coordinates":[],
		  "attestation":null,"attestor":{},"verification":"unverified",
		  "admitted-at":"2026-11-01T00:00:00Z","admission":{},
		  "supply-chain":{"state":"not-inventoried","reason":"no cataloger for this artifact shape",
		    "findings":0,"critical":0,"malicious":0,"verdict":null},
		  "packages":0,"sbom":null,"matches":[],"verdict-provenance":{},
		  "updated-at":"2026-11-01T00:00:00Z"}}}`))
	}))
	t.Cleanup(srv.Close)

	entry, err := newCacheClient(t, srv).GetArtifactLedgerEntry(context.Background(), "sha256:def")
	if err != nil {
		t.Fatalf("GetArtifactLedgerEntry: %v", err)
	}
	if entry.SupplyChain.State != "not-inventoried" {
		t.Errorf("state: %q", entry.SupplyChain.State)
	}
	if entry.SupplyChain.Reason == "" {
		t.Error("a not-inventoried entry must carry the reason, or the gap reads as a clean result")
	}
	if entry.SupplyChain.Verdict != nil || entry.SBOM != nil {
		t.Errorf("a null document must decode to nil, not an empty map: %+v / %+v",
			entry.SupplyChain.Verdict, entry.SBOM)
	}
	// A null attestation is an absence of record, and must not become the
	// string "null" or fail the decode.
	if entry.Attestation != "" {
		t.Errorf("attestation from null: %q", entry.Attestation)
	}
}

// The resource id is the digest, so it stands in when the attribute is absent.
// Without this a caller that asked BY digest could get an entry back with an
// empty one, and the identity of the thing it is holding would be gone.
func TestGetArtifactLedgerEntry_DigestFallsBackToTheResourceID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"id":"sha256:fromid","type":"artifact-ledger-entries",
		  "attributes":{"kind":"provider","verification":"unverified"}}}`))
	}))
	t.Cleanup(srv.Close)

	entry, err := newCacheClient(t, srv).GetArtifactLedgerEntry(context.Background(), "sha256:fromid")
	if err != nil {
		t.Fatalf("GetArtifactLedgerEntry: %v", err)
	}
	if entry.Digest != "sha256:fromid" {
		t.Errorf("digest: %q", entry.Digest)
	}
}

func TestGetArtifactLedgerEntry_NotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404","detail":"no ledger entry for that digest"}]}`))
	}))
	t.Cleanup(srv.Close)

	_, err := newCacheClient(t, srv).GetArtifactLedgerEntry(context.Background(), "sha256:missing")
	if err == nil {
		t.Fatal("expected an error on 404")
	}
	// Typed rather than merely non-nil: a caller distinguishing "never
	// admitted" from "the server is unwell" keys on this, and that distinction
	// is the point of a 404 here.
	var nf *NotFoundError
	if !errors.As(err, &nf) {
		t.Fatalf("expected *NotFoundError, got %T: %v", err, err)
	}
	if nf.Detail != "no ledger entry for that digest" {
		t.Errorf("the server's own message must survive: %q", nf.Detail)
	}
	// And the rendered message must name both, because NotFoundError falls
	// back to a bare "resource not found" when it carries neither -- which
	// from a digest-keyed lookup identifies nothing at all.
	if nf.Resource != "artifact-ledger-entry" || nf.ID != "sha256:missing" {
		t.Errorf("resource/id: %q / %q", nf.Resource, nf.ID)
	}
	if !strings.Contains(err.Error(), "sha256:missing") {
		t.Errorf("the message must name the digest asked for: %q", err.Error())
	}
}

func TestGetArtifactLedgerEntry_RequiresADigest(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("an empty digest must not reach the wire: the bare route is a different endpoint")
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	if _, err := newCacheClient(t, srv).GetArtifactLedgerEntry(context.Background(), ""); err == nil {
		t.Fatal("expected an error for an empty digest")
	}
}

// The package-cache listing grew the same two supply-chain answers (#2589), so
// an operator can see what is unverified or carrying findings without a ledger
// lookup per row.
func TestListPackageCache_CarriesVerificationAndSupplyChain(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
		  {"id":"pf-1","type":"cached-package-files","attributes":{
		     "ecosystem":"pypi","name":"flask","version":"3.0.0","filename":"flask-3.0.0.whl",
		     "size":1,"digest":"sha256:abc","cached-at":"","last-accessed-at":"",
		     "attestation":"upstream-publisher","verification":"verified",
		     "supply-chain":{"state":"findings","reason":null,"findings":2,"critical":1,
		       "malicious":0,"verdict":{"matcher":"trivy"}}}}
		],"meta":{"pagination":{"current-page":1,"page-size":1,"total-pages":1,"total-count":1}}}`))
	}))
	t.Cleanup(srv.Close)

	list, err := newCacheClient(t, srv).ListPackageCache(context.Background(), PackageCacheListOptions{})
	if err != nil {
		t.Fatalf("ListPackageCache: %v", err)
	}
	if len(list.Items) != 1 {
		t.Fatalf("expected one entry, got %d", len(list.Items))
	}
	got := list.Items[0]
	if got.Verification != "verified" {
		t.Errorf("verification: %q", got.Verification)
	}
	if got.SupplyChain.State != "findings" || got.SupplyChain.Critical != 1 {
		t.Errorf("supply chain: %+v", got.SupplyChain)
	}
	// The pre-existing attestation field must still decode: the two new
	// attributes sit beside it rather than replacing it.
	if got.Attestation != "upstream-publisher" {
		t.Errorf("attestation: %q", got.Attestation)
	}
}
