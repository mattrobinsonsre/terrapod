package terrapod

import (
	"context"
	"errors"
	"fmt"
	"net/url"
)

// The artifact ledger: what executed, and who vouched for each of it (#2589).
//
// A row is written at ADMISSION -- when an artifact is first accepted by a
// cache -- rather than by a scanner sweeping afterwards. So an artifact that
// was admitted and never inventoried still has an entry here; it is the
// inventory that is missing, not the record. That is the whole reason the
// ledger is separate from the cache listings: a cache row is evicted when
// retention reaps the bytes, and "what ran last quarter" does not stop being
// askable at that moment.
//
// # Keyed on the digest, which is why this takes one and not a name
//
// The digest is the artifact's identity; coordinates are a name somebody chose
// for it. One set of bytes reached under two names is ONE entry carrying two
// sets of coordinates, and two different artifacts published under one name are
// TWO entries -- which is the case a coordinate-keyed record cannot represent,
// and the one worth catching.
//
// So a caller holding a package name has to resolve it to a digest first (the
// package cache listing reports one per artifact) and then ask here. That extra
// hop is the modelling working, not an omission.
//
// # Two independent questions, and reading one for the other is the trap
//
// An entry answers two things that are easy to conflate:
//
//   - ATTESTATION and VERIFICATION -- who vouched for these bytes, and whether
//     that claim held. This is about provenance and is established at
//     admission.
//   - SUPPLY CHAIN -- what is inside the artifact and what is known about it.
//     This is derived from an inventory produced AFTER admission, and it can
//     legitimately be absent.
//
// An artifact can be perfectly attested and carry a critical finding, or be
// unattested and clean. Neither field predicts the other, and a reader that
// treats "verified" as "safe" is reading the first question's answer to the
// second one.
//
// This route requires platform admin.

// ArtifactSupplyChain is what is known about an artifact's contents.
//
// It is a separate type because the SAME block appears on the package-cache
// listing (see CachedPackageFile.SupplyChain), and the two must not be allowed
// to drift: an operator reading the listing and an auditor reading the ledger
// are asking one question and have to get one answer.
type ArtifactSupplyChain struct {
	// State is `inventoried` (an SBOM exists and nothing was found),
	// `findings` (an SBOM exists and something was), or `not-inventoried`.
	//
	// READ THIS BEFORE THE COUNTS. All three counts are zero under
	// `not-inventoried`, which is indistinguishable from a clean result if you
	// look only at the numbers -- and wrong in the reassuring direction. The
	// honest reading of a zero is only available with the state beside it:
	// "nothing found" and "nothing looked" are different facts and are recorded
	// as such on purpose.
	State string `json:"state"`
	// Reason says WHY there is no inventory, and is set only under
	// `not-inventoried`. An artifact can legitimately have none -- the
	// generator has no cataloger for that shape, the archive could not be
	// extracted within its bounds -- and recording which is what keeps a gap
	// from reading as an absence of evidence.
	Reason string `json:"reason"`
	// Findings is every match against the current vulnerability database
	// epoch, Critical the subset at that severity.
	Findings int `json:"findings"`
	Critical int `json:"critical"`
	// Malicious counts components flagged as known-malicious rather than
	// merely vulnerable, and is called out separately because the two warrant
	// different responses: a vulnerable dependency is a patch, a malicious one
	// is an incident.
	Malicious int `json:"malicious"`
	// Verdict is the summary the matcher produced, nil when there is none.
	// Shapeless here on purpose: it is the tool's own document, and freezing a
	// Go struct over it would make every upstream field addition a change to
	// this SDK's exported surface.
	Verdict map[string]any `json:"verdict"`
}

// ArtifactLedgerEntry is one artifact the platform has admitted, by digest.
//
// There is no separate ID field: the digest IS the resource id, and a second
// field holding the same string is a pair that can be set inconsistently by a
// future decoder while nothing notices.
type ArtifactLedgerEntry struct {
	// Digest is `sha256:<hex>` and is the identity of these bytes.
	Digest string `json:"digest"`
	// Kind is which cache served it -- `package`, `binary`, `provider`. Not a
	// reference to a row in that cache: the entry must outlive the cached
	// artifact, so it names the cache rather than pointing into it.
	Kind string `json:"kind"`
	// Coordinates is every naming these bytes have been served under, as each
	// ecosystem spells its own. A list, because one digest legitimately arrives
	// under several names, and MORE THAN ONE ENTRY IS NOT AN ANOMALY.
	//
	// Each is a map rather than a string so the parts stay queryable and a new
	// kind can name itself however it needs to, instead of every reader
	// learning a different delimiter convention.
	Coordinates []map[string]any `json:"coordinates"`
	// Attestation is HOW this artifact came to be trusted:
	// `upstream-publisher` (the publisher's own signature or attestation held),
	// `upstream-digest` (the bytes matched the digest the index published),
	// `none-published` (that ecosystem publishes nothing to check against,
	// recorded honestly rather than implied), or `operator-upload` (a named
	// person vouched for it instead).
	//
	// EMPTY means the server recorded none, which is not the same as
	// "unverified" -- it is an absence of record rather than a negative result.
	Attestation string `json:"attestation"`
	// Attestor is the detail behind Attestation, shaped by which kind it is: a
	// publisher identity, issuer and predicate for an upstream attestation; the
	// uploading user, time and any approval reference for an operator upload.
	//
	// Untyped because the kinds share no fields, and a struct whose half is
	// always zero reads as missing data rather than as inapplicable.
	Attestor map[string]any `json:"attestor"`
	// Verification is whether the attestation's claim HELD: `verified`,
	// `unverified` or `failed`.
	//
	// The distinction that matters is between the last two, and it is why there
	// are three values rather than a boolean. `unverified` says nothing was
	// checked; `failed` says a check ran and did not hold. Collapsed into one
	// flag, an artifact nobody could check would read exactly like one that
	// failed a check -- and only the second is a finding.
	Verification string `json:"verification"`
	// AdmittedAt is when these bytes were FIRST accepted, RFC3339. It is not
	// re-stamped when the same digest is later served under a new name: the
	// first admission is the one that happened, and a later pull does not
	// re-decide when it did.
	AdmittedAt string `json:"admitted-at"`
	// Admission records which policy admitted it, so a reviewer can tell an
	// artifact admitted under today's rules from one grandfathered in.
	Admission map[string]any `json:"admission"`
	// SupplyChain is what is known about the contents. Independent of
	// Attestation and Verification -- see the file comment.
	SupplyChain ArtifactSupplyChain `json:"supply-chain"`
	// Packages is how many components the inventory found. Zero means nothing
	// was found OR nothing was inventoried; SupplyChain.State is what tells
	// those apart.
	Packages int `json:"packages"`
	// SBOM is the CycloneDX document the inventory produced, nil when there is
	// none. It can be large -- a component per dependency -- so a caller
	// displaying an entry should decide deliberately whether to carry it.
	//
	// Left as a map rather than a typed document: CycloneDX is an upstream
	// schema that grows, and a Go struct over it would silently drop whatever
	// it had not been taught about.
	SBOM map[string]any `json:"sbom"`
	// Matches is the findings behind SupplyChain's counts, each the matcher's
	// own record. Re-derivable from the SBOM, so a stale list here is never the
	// only copy of anything.
	Matches []map[string]any `json:"matches"`
	// VerdictProvenance is what makes a finding -- or the absence of one --
	// reproducible rather than merely asserted: tool versions, the
	// vulnerability-database epoch, a settings fingerprint and timestamps.
	//
	// Read it before quoting a clean result. Without it, "no findings" is
	// undated and unattributable, and an epoch from six months ago is a
	// different claim from one from this morning.
	VerdictProvenance map[string]any `json:"verdict-provenance"`
	// UpdatedAt is when the entry last changed, RFC3339 -- a new coordinate, a
	// strengthened attestation, or a re-match against a newer database epoch.
	UpdatedAt string `json:"updated-at"`
}

// GetArtifactLedgerEntry reads the ledger entry for one digest. Requires
// platform admin.
//
// digest is the full `sha256:<hex>` form, exactly as the caches report it --
// not the bare hex. It travels as a path segment and needs no encoding of its
// own: a colon is legal there, so the digest reaches the server verbatim and
// the escaping below is for anything unexpected rather than for the normal
// case.
//
// Returns *NotFoundError when NOTHING WAS EVER ADMITTED UNDER THIS DIGEST,
// which is a meaningful answer rather than merely an absent one: every artifact
// a runner is served is recorded here, so a 404 for a digest something
// genuinely executed is itself the finding.
func (c *Client) GetArtifactLedgerEntry(ctx context.Context, digest string) (*ArtifactLedgerEntry, error) {
	if digest == "" {
		return nil, errors.New("artifact digest is required")
	}
	data, err := c.Get(ctx, "/api/v1/admin/artifact-ledger/"+url.PathEscape(digest))
	if err != nil {
		// Re-wrapped with the resource and the digest, because NotFoundError's
		// Error() renders a bare "resource not found" when it carries neither
		// -- which from a digest-keyed lookup says neither what was missing nor
		// which digest was asked for. The Detail is carried through, so nothing
		// the server said is lost, and the type is preserved so errors.As still
		// answers for callers keying on it.
		var nf *NotFoundError
		if errors.As(err, &nf) {
			return nil, &NotFoundError{
				Resource: "artifact-ledger-entry", ID: digest, Detail: nf.Detail,
			}
		}
		return nil, err
	}
	resource, err := ParseResource(data)
	if err != nil {
		return nil, fmt.Errorf("parse artifact ledger entry: %w", err)
	}
	var entry ArtifactLedgerEntry
	if err := decodeResourceAttributes(resource.Attributes, &entry); err != nil {
		return nil, fmt.Errorf("decode artifact ledger entry %q: %w", digest, err)
	}
	// The resource id is the digest, so it stands in when the attribute is
	// absent -- a caller that asked by digest must never get an entry back with
	// an empty one.
	if entry.Digest == "" {
		entry.Digest = resource.ID
	}
	return &entry, nil
}
