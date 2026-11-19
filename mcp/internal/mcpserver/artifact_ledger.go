package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerArtifactLedger adds the one read over the artifact ledger (#2589):
// what executed, and who vouched for each of it.
//
// # Its own group rather than part of the cache admin tools
//
// The ledger outlives the caches. An entry is written at admission and is NOT
// removed when retention reaps the bytes, because "what ran last quarter" does
// not stop being askable at that moment. Grouping it with the cache listings
// would suggest it answers for what is currently cached, which is the one thing
// it deliberately does not do.
//
// # Keyed on the digest, so an agent usually needs two calls
//
// There is no lookup by package name here, and that is the modelling rather
// than a gap: one set of bytes served under two names is ONE entry with two
// coordinates, and two different artifacts published under one name are TWO
// entries. A name cannot address either case unambiguously. An agent holding a
// name resolves it with terrapod_package_cache_list, which reports a digest per
// artifact, and asks here with that.
//
// The description says so explicitly, because an agent that cannot see why will
// otherwise read the missing name filter as the tool being incomplete and
// either guess a digest or give up.
func registerArtifactLedger(s *mcp.Server, c *terrapod.Client) {
	registerArtifactLedgerGet(s, c)
}

// ── terrapod_artifact_ledger_get ──────────────────────────────────────────

type artifactSupplyChainOut struct {
	// State is inventoried, findings, or not-inventoried, and is the field to
	// read BEFORE the three counts below: all of them are zero under
	// not-inventoried, which is indistinguishable from a clean result if you
	// look only at the numbers.
	State string `json:"state"`
	// Reason says why there is no inventory, under not-inventoried only.
	Reason string `json:"reason,omitempty"`
	// Findings is every match against the current vulnerability-database
	// epoch; Critical the subset at that severity.
	Findings int `json:"findings"`
	Critical int `json:"critical"`
	// Malicious is counted apart from Findings because the two warrant
	// different responses: a vulnerable dependency is a patch, a malicious one
	// is an incident.
	Malicious int            `json:"malicious"`
	Verdict   map[string]any `json:"verdict,omitempty"`
}

type artifactLedgerOut struct {
	Digest string `json:"digest"`
	Kind   string `json:"kind"`
	// Coordinates is every naming these bytes were served under. More than one
	// is normal rather than an anomaly -- the same artifact legitimately
	// arrives under several names.
	Coordinates []map[string]any `json:"coordinates,omitempty"`

	// Attestation and Attestor say WHO vouched; Verification says whether the
	// claim held. Separate fields because an attestation that was attempted
	// and did not hold is a finding, where one that could not be attempted is
	// not.
	Attestation  string         `json:"attestation,omitempty"`
	Attestor     map[string]any `json:"attestor,omitempty"`
	Verification string         `json:"verification,omitempty"`

	AdmittedAt string         `json:"admitted_at,omitempty"`
	Admission  map[string]any `json:"admission,omitempty"`
	UpdatedAt  string         `json:"updated_at,omitempty"`

	SupplyChain artifactSupplyChainOut `json:"supply_chain"`
	// Packages is how many components the inventory found. Zero means nothing
	// was found OR nothing was inventoried; supply_chain.state tells them
	// apart.
	Packages int `json:"packages"`
	// Matches is the findings behind the counts, returned in full: they are
	// what the caller came for and are bounded by how many there are.
	Matches []map[string]any `json:"matches,omitempty"`
	// VerdictProvenance dates and attributes the verdict -- tool versions, the
	// vulnerability-database epoch, a settings fingerprint. Without it "no
	// findings" is an undated assertion.
	VerdictProvenance map[string]any `json:"verdict_provenance,omitempty"`

	// SBOM is returned only when asked for, because it carries a component per
	// dependency and can be far larger than everything else here together.
	SBOM map[string]any `json:"sbom,omitempty"`
	// SBOMOmitted distinguishes THE TWO REASONS the field above can be absent:
	// true means a document exists and was not requested, false-with-no-sbom
	// means none has been produced. Without it an agent cannot tell "you did
	// not ask" from "there is nothing", and the second is a finding where the
	// first is not.
	SBOMOmitted bool `json:"sbom_omitted,omitempty"`
}

func registerArtifactLedgerGet(s *mcp.Server, c *terrapod.Client) {
	type artifactLedgerGetIn struct {
		Digest      string `json:"digest" jsonschema:"the artifact's digest in full sha256:<hex> form, exactly as the caches report it, NOT the bare hex and NOT a package name. Get one from terrapod_package_cache_list, which reports a digest per cached artifact. The ledger is keyed on the digest because that is the artifact's identity: one set of bytes served under two names is one entry carrying both namings, and two different artifacts published under one name are two entries, which is the case a name cannot address"`
		IncludeSBOM bool   `json:"include_sbom,omitempty" jsonschema:"return the full CycloneDX inventory document as well. Off by default because it carries a component per dependency and is routinely far larger than every other field together. The packages count and the supply_chain findings answer most questions without it, so ask for it only when you need the component list itself"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_artifact_ledger_get",
		Description: "Look up one artifact in Terrapod's artifact ledger: the durable record of everything a runner has ever been served, and who vouched for each of it. This is the tool for \"what executed, and can we account for it\" -- a compliance or incident question rather than a capacity one. A row is written at ADMISSION, when the artifact is first accepted, so it SURVIVES the artifact being evicted from the cache: an entry here for bytes the package cache no longer holds is normal and is the point, because \"what ran last quarter\" does not stop being askable when retention reaps the file. " +
			"TAKES A DIGEST, NOT A NAME. Pass the full sha256:<hex> as the caches report it; resolve a package name to one with terrapod_package_cache_list first. That is the modelling rather than a missing filter: the digest is the artifact's identity, so one set of bytes served under two names is ONE entry carrying both namings (several `coordinates` is normal, not an anomaly), and two different artifacts published under one name are TWO entries -- the case a name-keyed lookup cannot represent and the one most worth catching. " +
			"IT ANSWERS TWO INDEPENDENT QUESTIONS AND READING ONE FOR THE OTHER IS THE MISTAKE TO AVOID. (1) PROVENANCE, established at admission: `attestation` is what was claimed -- `upstream-publisher` (the publisher's own signature or attestation held), `upstream-digest` (the bytes matched the digest the index published), `none-published` (that ecosystem publishes nothing to check against, recorded honestly rather than implied), `operator-upload` (a named person vouched for it instead, with `attestor` carrying who and on what authority) -- and `verification` is what came of checking it. Of its three values the one that is a finding is `failed`: a check ran and did not hold. `unverified` means nothing was checked, which is a weaker and very different statement, and an EMPTY `attestation` is an absence of record rather than a negative result. (2) CONTENTS, derived from an inventory produced AFTER admission and reported under `supply_chain`. " +
			"An artifact can be perfectly attested and carry a critical finding, or be unattested and clean. Neither predicts the other, so never report \"verified\" as \"safe\". " +
			"READ `supply_chain.state` BEFORE ITS COUNTS. `findings`, `critical` and `malicious` are ALL ZERO under `not-inventoried`, which looks exactly like a clean artifact and is wrong in the reassuring direction -- it means nothing looked, not that nothing was found, and `supply_chain.reason` says why (no cataloger for that shape, an archive that could not be extracted in bounds). Only `inventoried` with zero counts is a clean result. `malicious` is counted apart from `findings` on purpose, because the two warrant different responses: a vulnerable dependency is a patch, a known-malicious one is an incident. Before quoting a clean result, read `verdict_provenance` -- it carries the tool versions and the vulnerability-database epoch that make the verdict reproducible, and a verdict from a six-month-old epoch is a different claim from one from this morning. " +
			"The full CycloneDX document is returned only with `include_sbom`, since it holds a component per dependency; `sbom_omitted` tells you a document exists and was not requested, as distinct from none existing at all. " +
			"A NOT-FOUND IS ITSELF AN ANSWER: every artifact a runner is served is recorded here, so no entry for a digest something genuinely executed is the finding, not an empty result to shrug at. Read-only; requires platform admin.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in artifactLedgerGetIn) (*mcp.CallToolResult, artifactLedgerOut, error) {
		entry, err := c.GetArtifactLedgerEntry(ctx, in.Digest)
		if err != nil {
			return errResult(err), artifactLedgerOut{}, nil
		}

		out := artifactLedgerOut{
			Digest:       entry.Digest,
			Kind:         entry.Kind,
			Coordinates:  entry.Coordinates,
			Attestation:  entry.Attestation,
			Attestor:     entry.Attestor,
			Verification: entry.Verification,
			AdmittedAt:   entry.AdmittedAt,
			Admission:    entry.Admission,
			UpdatedAt:    entry.UpdatedAt,
			SupplyChain: artifactSupplyChainOut{
				State:     entry.SupplyChain.State,
				Reason:    entry.SupplyChain.Reason,
				Findings:  entry.SupplyChain.Findings,
				Critical:  entry.SupplyChain.Critical,
				Malicious: entry.SupplyChain.Malicious,
				Verdict:   entry.SupplyChain.Verdict,
			},
			Packages:          entry.Packages,
			Matches:           entry.Matches,
			VerdictProvenance: entry.VerdictProvenance,
		}
		// Keyed on the document EXISTING rather than on the flag alone, so a
		// caller that did not ask is told there is something to ask for only
		// when there is.
		if in.IncludeSBOM {
			out.SBOM = entry.SBOM
		} else {
			out.SBOMOmitted = entry.SBOM != nil
		}
		return nil, out, nil
	})
}
