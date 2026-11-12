package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
)

// Warming the package and container caches ahead of a seal (#1420, every
// ecosystem since #2486).
//
// Warming is how a restricted-network deployment is made practical: fill the
// caches while upstream is still reachable, then seal the node. The binary and
// provider caches have WarmCacheBulk; these are the other two, and they differ
// from it in one way a caller has to know about.
//
// # It is ASYNCHRONOUS, and that is not an implementation detail
//
// A real dependency closure outlives an HTTP request, so a submission returns a
// JOB ID and the work happens on the server's scheduler. WarmPackages and
// WarmImages hand back a WarmSubmission; WarmJobStatus polls it. A caller that
// treats the 202 as "done" has learned only that the server accepted the list.
//
// # Three things the outcomes say that a count would not
//
//   - PER ITEM. "Failed" over a submission of twenty is not actionable, and
//     finding the specific gaps before sealing is the entire point.
//   - ALREADY CACHED IS NOT FETCHED. WarmItemOutcome.Fetched is how many files
//     this run pulled and Files how many the item covers, so Fetched == 0 with
//     Files > 0 means the item was already held. Warming is idempotent and
//     resumable by design — re-running is the normal way to close the last gaps
//     — and without the distinction the second run reports exactly what the
//     first did, so a caller cannot see that it converged.
//   - WHERE IT CAME FROM. Source is the base the item was pulled from, which is
//     not always the serving upstream (see PackageWarmSpec.Source).
//
// # A sealed node answers 409, not an empty result
//
// Warming is an upstream fetch by definition, so registry.cache_only makes it
// impossible rather than merely empty. The server refuses with 409 and says so,
// because "0 succeeded" on a sealed node looks exactly like a set of missing
// packages and sends an operator looking upstream for a problem on this side.
// Read it as *ConflictError, never as "nothing needed warming".
//
// Every route here requires platform admin.

// MaxWarmItems is the server's cap on one submission. PackageCacheSources
// reports the live value in its meta; this is what the server ships with.
const MaxWarmItems = 500

// PackageWarmSpec names one artifact to warm.
type PackageWarmSpec struct {
	// Ecosystem is one of galaxy, pypi, npm, go, nuget, pulumi.
	Ecosystem string `json:"ecosystem"`
	// Name is the artifact as its own ecosystem names it: "namespace.collection"
	// for galaxy, a module path for go, a package id for nuget and pypi,
	// "[kind:]plugin" for pulumi.
	Name string `json:"name"`
	// Version SHOULD be explicit. That is what makes a warm reproducible and
	// checkable against a lockfile — "warm the latest" cannot be either.
	//
	// Empty resolves to whatever that registry calls newest, as a convenience,
	// and PULUMI REFUSES IT: its download protocol publishes no index, so there
	// is nothing to resolve a bare name against. PackageCacheSource.ResolvesLatest
	// says which ecosystems accept it.
	Version string `json:"version,omitempty"`
	// Source is a LABEL in registry.package_cache.<ecosystem>.mirrors naming
	// where to warm FROM, instead of the serving upstream — a sibling Terrapod
	// or an internal mirror. Empty means the upstream.
	//
	// A label and never a URL: the configured upstream is the whole of that
	// proxy's request-forgery surface precisely because it is operator-set, so a
	// request body carrying a URL would hand an admin-authenticated caller a
	// server-side fetch to any host they name. An undeclared label is a 422 that
	// names the ones that are declared; PackageCacheSources lists them.
	Source string `json:"source,omitempty"`
	// Platforms is "os/arch" pairs, for PULUMI ONLY — a plugin is published per
	// platform. Empty warms the runner's own (linux/amd64 and linux/arm64);
	// naming one leaves the other to be discovered after sealing. Ignored by the
	// other five, which have one artifact set per version.
	Platforms []string `json:"platforms,omitempty"`
}

// WarmSubmission is what a warm POST hands back: something to poll, not a
// result.
type WarmSubmission struct {
	JobID  string `json:"-"`
	Status string `json:"status"`
	Total  int    `json:"total"`
}

// WarmItemOutcome is what became of one submitted item.
type WarmItemOutcome struct {
	Ecosystem string `json:"ecosystem"`
	Ref       string `json:"ref"`
	OK        bool   `json:"ok"`
	// Detail carries the reason on a failure, and on a PARTIAL success names
	// what did not land — a pulumi item warming two platforms reports OK with
	// the failing platform here rather than reporting a clean success.
	Detail string `json:"detail"`
	// Files is how many files the item covers.
	Files int `json:"files"`
	// Fetched is how many of them this run pulled. Zero against a non-zero
	// Files is "already cached" — see the package comment.
	Fetched int `json:"fetched"`
	// Source is the base these bytes came from.
	Source string `json:"source"`
}

// WarmJobState is a submission's progress and its per-item report.
type WarmJobState struct {
	JobID       string            `json:"-"`
	Status      string            `json:"status"` // "queued" | "running" | "finished"
	Total       int               `json:"total"`
	Completed   int               `json:"completed"`
	Succeeded   int               `json:"succeeded"`
	Failed      int               `json:"failed"`
	SubmittedAt string            `json:"submitted-at"`
	Outcomes    []WarmItemOutcome `json:"outcomes"`
}

// Finished reports whether there is any point polling again.
func (s *WarmJobState) Finished() bool { return s.Status == "finished" }

// PackageCacheSource is where a warm of one ecosystem would pull from.
type PackageCacheSource struct {
	Ecosystem string `json:"ecosystem"`
	// Enabled is whether that proxy serves at all. A warm for a disabled one is
	// a 404.
	Enabled bool `json:"enabled"`
	// Upstream is the default this ecosystem pulls through. EMPTY for "oci",
	// which has no single answer: an image reference's own first path component
	// names the host, chosen against registry.oci.upstreams.
	Upstream string `json:"upstream"`
	// Mirrors are the labels a PackageWarmSpec.Source may name. For "oci" this
	// is the upstream map instead, keyed by the host a reference starts with.
	Mirrors map[string]string `json:"mirrors"`
	// ResolvesLatest is whether a version may be omitted. False for pulumi, and
	// that is a property of the protocol rather than of the deployment.
	ResolvesLatest bool `json:"resolves-latest"`
}

// PackageCacheSourcesMeta accompanies the per-ecosystem list.
type PackageCacheSourcesMeta struct {
	// MaxItems is the server's live cap on one submission.
	MaxItems int `json:"max-items"`
	// Sealed is true when registry.cache_only forbids reaching upstream, so
	// every warm submission can only answer 409. Reported HERE as well as on
	// that refusal because a caller needs it BEFORE it asks — a UI offering a
	// form whose submission cannot succeed is worse than one that says why.
	Sealed bool `json:"sealed"`
}

// PackageCacheSources reports where a warm of each ecosystem would pull from,
// before one is submitted.
//
// The visibility half of #2486: an operator could see neither which upstream a
// submission would reach nor that a nearer one had been declared, so the only
// feedback was that bytes had arrived from somewhere — which on a node about to
// be sealed is the one thing worth being sure of. Requires platform admin.
func (c *Client) PackageCacheSources(ctx context.Context) ([]PackageCacheSource, PackageCacheSourcesMeta, error) {
	data, err := c.Get(ctx, "/api/v1/admin/package-cache/sources")
	if err != nil {
		return nil, PackageCacheSourcesMeta{}, err
	}
	var doc struct {
		Data []struct {
			Attributes PackageCacheSource `json:"attributes"`
		} `json:"data"`
		Meta PackageCacheSourcesMeta `json:"meta"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, PackageCacheSourcesMeta{}, fmt.Errorf("parse package cache sources: %w", err)
	}
	out := make([]PackageCacheSource, 0, len(doc.Data))
	for _, d := range doc.Data {
		out = append(out, d.Attributes)
	}
	return out, doc.Meta, nil
}

// WarmPackages submits package artifacts to be cached and returns a job to
// poll. See the package comment: the 202 means accepted, not done.
//
// Returns *ConflictError on a sealed node (registry.cache_only), which is a
// configuration that forbids fetching rather than an empty result. Requires
// platform admin.
func (c *Client) WarmPackages(ctx context.Context, specs []PackageWarmSpec) (*WarmSubmission, error) {
	if len(specs) == 0 {
		return nil, errors.New("at least one package is required")
	}
	return c.submitWarm(ctx, "/api/v1/admin/package-cache/warm", map[string]any{"packages": specs})
}

// WarmImages pulls images through the container mirror ahead of a seal,
// manifest and every blob they reference.
//
// Each reference is "host/repo:tag", exactly as a client would pull it; an
// absent tag means "latest". Blobs are included deliberately — a manifest-only
// warm would look cached and fail on the first layer a sealed node tried to
// serve. Requires platform admin.
func (c *Client) WarmImages(ctx context.Context, references []string) (*WarmSubmission, error) {
	if len(references) == 0 {
		return nil, errors.New("at least one image reference is required")
	}
	return c.submitWarm(ctx, "/api/v1/admin/oci/warm", map[string]any{"images": references})
}

func (c *Client) submitWarm(ctx context.Context, path string, body map[string]any) (*WarmSubmission, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal warm submission: %w", err)
	}
	data, err := c.Post(ctx, path, payload)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			ID         string         `json:"id"`
			Attributes WarmSubmission `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse warm submission: %w", err)
	}
	out := doc.Data.Attributes
	out.JobID = doc.Data.ID
	return &out, nil
}

// WarmJobStatus reports a submission's progress and its per-item outcomes.
//
// Poll until Finished(). The report is the deliverable: one item failing never
// stops the rest, so a finished job with failures has still told you about
// every gap rather than only the first. Returns *NotFoundError once the job has
// aged out of the server's day-long window. Requires platform admin.
func (c *Client) WarmJobStatus(ctx context.Context, jobID string) (*WarmJobState, error) {
	if jobID == "" {
		return nil, errors.New("warm job id is required")
	}
	data, err := c.Get(ctx, "/api/v1/admin/warm-jobs/"+url.PathEscape(jobID))
	if err != nil {
		return nil, err
	}
	var doc struct {
		Data struct {
			ID         string       `json:"id"`
			Attributes WarmJobState `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse warm job: %w", err)
	}
	out := doc.Data.Attributes
	out.JobID = doc.Data.ID
	return &out, nil
}
