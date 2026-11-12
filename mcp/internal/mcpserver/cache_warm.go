package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerCacheWarm adds the three tools that FILL the caches, beside the six
// that inspect and empty them (#1420, #2486). All three are platform-admin.
//
// # Why an agent gets these at all
//
// The cache-admin group next door can tell an agent that a cache is cold; this
// is what lets it do something about that. Warming is additive -- it fetches
// things the deployment already fetches on demand, into a cache the deployment
// already keeps -- so the worst outcome of an unnecessary warm is bytes in the
// object store that retention would have reclaimed anyway. That is why the two
// submissions are `mutating` rather than `destructive`: nothing is removed and
// nothing a run depends on is changed.
//
// The one real cost is upstream's patience and the operator's bandwidth, which
// is why the submission bound (500 items) is reported by
// terrapod_cache_warm_sources rather than left to be discovered as a 422.
//
// # The shape an agent has to get right, said in every description
//
// A submission is ACCEPTED, not done: the POST returns a job id because a real
// closure outlives a request. An agent that reports success off the 202 has
// said only that the server took the list. terrapod_cache_warm_status is the
// other half and the descriptions point at it.
//
// # Why there are two submission tools and not one
//
// `terrapod_cache_warm_packages` and `terrapod_cache_warm_images` take
// genuinely different arguments -- a package is an ecosystem, a name, a version
// and optionally a source, while an image is one reference string whose host
// chooses its own upstream -- and collapsing them would make a tool whose
// arguments are half-ignored depending on a discriminator. They share a status
// tool because a job is a job.
func registerCacheWarm(s *mcp.Server, c *terrapod.Client) {
	registerCacheWarmSources(s, c)
	registerCacheWarmPackages(s, c)
	registerCacheWarmImages(s, c)
	registerCacheWarmStatus(s, c)
}

// ── terrapod_cache_warm_sources ────────────────────────────────────────────

type cacheWarmSourceOut struct {
	Ecosystem string `json:"ecosystem"`
	// Enabled false means that proxy does not serve, so a warm for it is a 404.
	Enabled bool `json:"enabled"`
	// Upstream is the default registry this ecosystem pulls through. EMPTY for
	// `oci`, which has no single answer: an image reference's first path
	// component names its own host.
	Upstream string `json:"upstream,omitempty"`
	// Mirrors are the labels `source` may name on a warm submission. For `oci`
	// these are the hosts a reference may start with instead.
	Mirrors map[string]string `json:"mirrors,omitempty"`
	// ResolvesLatest false means a version MUST be given. True for everything
	// but pulumi, whose download protocol publishes no index at all.
	ResolvesLatest bool `json:"resolves_latest"`
}

type cacheWarmSourcesOut struct {
	Ecosystems []cacheWarmSourceOut `json:"ecosystems"`
	// MaxItems is the cap on one submission.
	MaxItems int `json:"max_items"`
	// Sealed true means `registry.cache_only` forbids reaching upstream, so
	// every warm submission can only fail with a conflict. Check this FIRST:
	// a sealed node is a configuration, not a transient problem, and telling
	// the user to retry would send them looking at their network.
	Sealed bool `json:"sealed"`
}

func registerCacheWarmSources(s *mcp.Server, c *terrapod.Client) {
	type cacheWarmSourcesIn struct{}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_cache_warm_sources",
		Description: "Report where a cache warm would pull from, and whether warming is possible at all, BEFORE submitting one. Call this first whenever you are about to warm anything. Three things here change what a correct submission looks like. (1) `sealed` true means `registry.cache_only` forbids this node from reaching upstream, so every warm can only answer 409 -- that is a deliberate configuration for an air-gapped deployment, NOT a fault, so report it and stop rather than suggesting a retry. (2) `resolves_latest` false, which is the case for `pulumi`, means a version is REQUIRED: that download protocol publishes no index, so there is nothing to resolve a bare name against and a submission without one is refused. (3) `mirrors` lists the labels this deployment has declared as alternative places to warm FROM -- a sibling Terrapod or an internal mirror -- which is what `source` on a warm submission names. `source` takes one of these LABELS and never a URL, so if the mirror the user wants is not listed here it has to be added to the deployment's configuration (`registry.package_cache.<ecosystem>.mirrors`) first; do not try to pass a URL. Also reports `max_items`, the cap on one submission. Read-only; requires platform admin.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ cacheWarmSourcesIn) (*mcp.CallToolResult, cacheWarmSourcesOut, error) {
		sources, meta, err := c.PackageCacheSources(ctx)
		if err != nil {
			return errResult(err), cacheWarmSourcesOut{}, nil
		}
		out := cacheWarmSourcesOut{MaxItems: meta.MaxItems, Sealed: meta.Sealed}
		for _, s := range sources {
			out.Ecosystems = append(out.Ecosystems, cacheWarmSourceOut{
				Ecosystem:      s.Ecosystem,
				Enabled:        s.Enabled,
				Upstream:       s.Upstream,
				Mirrors:        s.Mirrors,
				ResolvesLatest: s.ResolvesLatest,
			})
		}
		return nil, out, nil
	})
}

// ── terrapod_cache_warm_packages ───────────────────────────────────────────

type cacheWarmSubmissionOut struct {
	// JobID is what terrapod_cache_warm_status takes. Without polling it, a
	// submission has told you only that the server accepted the list.
	JobID  string `json:"job_id"`
	Status string `json:"status"`
	Total  int    `json:"total"`
}

func registerCacheWarmPackages(s *mcp.Server, c *terrapod.Client) {
	type cacheWarmPackageIn struct {
		Ecosystem string   `json:"ecosystem" jsonschema:"which proxy to warm: galaxy, pypi, npm, go, nuget or pulumi"`
		Name      string   `json:"name" jsonschema:"the artifact as its own ecosystem names it: namespace.collection for galaxy (community.general), a module path for go (github.com/spf13/cobra), a package id for pypi and nuget, a package name for npm, and plugin or kind:plugin for pulumi (aws, or language:python)"`
		Version   string   `json:"version,omitempty" jsonschema:"the EXACT version to warm, which is what makes the result checkable against a lockfile. Omitting it takes whatever the registry calls newest and is REFUSED for pulumi"`
		Source    string   `json:"source,omitempty" jsonschema:"a mirror LABEL from terrapod_cache_warm_sources to pull from instead of the default upstream. Never a URL: an undeclared label is refused"`
		Platforms []string `json:"platforms,omitempty" jsonschema:"os/arch pairs for PULUMI ONLY, whose plugins are published per platform. Omit for the runner's own linux/amd64 and linux/arm64. Ignored by every other ecosystem"`
	}
	type cacheWarmPackagesIn struct {
		Packages []cacheWarmPackageIn `json:"packages" jsonschema:"the artifacts to cache. Name versions explicitly wherever you know them"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_cache_warm_packages",
		Description: "Pre-populate Terrapod's package caches -- galaxy, pypi, npm, go, nuget and pulumi -- so a later run does not spend minutes filling a cold cache, and so a deployment that is about to be sealed off from the internet already holds what it needs. This is the companion to terrapod_package_cache_list: that one says what is missing, this one fills it. GIVE EXPLICIT VERSIONS wherever you know them, ideally the exact set a lockfile or a requirements file pins: a warm named at a version can afterwards be checked against that file, while \"warm the latest\" cannot be, and on a node that is about to be sealed an unverifiable warm is the thing that gets discovered too late. An unversioned item is a convenience, not the normal way to ask, and `pulumi` refuses it outright. CALL terrapod_cache_warm_sources FIRST: it says whether the node is sealed (in which case this can only fail), which ecosystems need an explicit version, and which mirror labels `source` may name. THIS RETURNS A JOB ID, NOT A RESULT -- warming a real dependency closure outlives one request, so the work runs in the background and terrapod_cache_warm_status reports per-item outcomes. Do not report success from this call alone; poll until the job is finished, then read the outcomes, because the per-item gaps are the whole point. Warming is idempotent: items already held are skipped and re-running retries only the rest, so running it twice is safe and is the normal way to close the last gaps. Requires platform admin.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in cacheWarmPackagesIn) (*mcp.CallToolResult, cacheWarmSubmissionOut, error) {
		specs := make([]terrapod.PackageWarmSpec, 0, len(in.Packages))
		for _, p := range in.Packages {
			specs = append(specs, terrapod.PackageWarmSpec{
				Ecosystem: p.Ecosystem,
				Name:      p.Name,
				Version:   p.Version,
				Source:    p.Source,
				Platforms: p.Platforms,
			})
		}
		sub, err := c.WarmPackages(ctx, specs)
		if err != nil {
			return errResult(err), cacheWarmSubmissionOut{}, nil
		}
		return nil, cacheWarmSubmissionOut{JobID: sub.JobID, Status: sub.Status, Total: sub.Total}, nil
	})
}

// ── terrapod_cache_warm_images ─────────────────────────────────────────────

func registerCacheWarmImages(s *mcp.Server, c *terrapod.Client) {
	type cacheWarmImagesIn struct {
		Images []string `json:"images" jsonschema:"image references to mirror, each host/repository:tag exactly as a client would pull it. An omitted tag means latest"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_cache_warm_images",
		Description: "Pull container images through Terrapod's mirror ahead of time -- manifest and every blob they reference -- so an execution-environment image is already held when a configure needs it, and so a node that is about to be sealed can still serve it. Each reference is host/repository:tag exactly as a client would pull it, and the host's first path component must name an upstream the deployment has configured (terrapod_cache_warm_sources lists them under the oci entry); an unconfigured host is reported per item rather than failing the submission. Blobs are included deliberately: a manifest-only warm would look cached and then fail on the first layer a sealed node tried to serve. THIS RETURNS A JOB ID, NOT A RESULT -- pulling images outlives one request, so poll terrapod_cache_warm_status until the job is finished and read the per-item outcomes before reporting anything. Check terrapod_cache_warm_sources first for `sealed`: a sealed node cannot fetch at all and this can only fail there. Requires platform admin.",
		Annotations: mutating,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in cacheWarmImagesIn) (*mcp.CallToolResult, cacheWarmSubmissionOut, error) {
		sub, err := c.WarmImages(ctx, in.Images)
		if err != nil {
			return errResult(err), cacheWarmSubmissionOut{}, nil
		}
		return nil, cacheWarmSubmissionOut{JobID: sub.JobID, Status: sub.Status, Total: sub.Total}, nil
	})
}

// ── terrapod_cache_warm_status ─────────────────────────────────────────────

type cacheWarmOutcomeOut struct {
	Ecosystem string `json:"ecosystem"`
	Ref       string `json:"ref"`
	OK        bool   `json:"ok"`
	// Detail is the reason on a failure, and on a partial success names what
	// did not land -- a pulumi item covering two platforms reports ok with the
	// failing one here.
	Detail string `json:"detail,omitempty"`
	// Files is how many files the item covers; Fetched how many THIS run
	// pulled. Fetched 0 with Files above 0 is "it was already cached", which
	// is a different finding from "it was fetched" and the one that says a
	// repeated submission has converged.
	Files   int    `json:"files"`
	Fetched int    `json:"fetched"`
	Source  string `json:"source,omitempty"`
}

type cacheWarmStatusOut struct {
	JobID string `json:"job_id"`
	// Status is queued, running or finished. Only finished means the outcomes
	// below are the whole report.
	Status      string                `json:"status"`
	Finished    bool                  `json:"finished"`
	Total       int                   `json:"total"`
	Completed   int                   `json:"completed"`
	Succeeded   int                   `json:"succeeded"`
	Failed      int                   `json:"failed"`
	SubmittedAt string                `json:"submitted_at,omitempty"`
	Outcomes    []cacheWarmOutcomeOut `json:"outcomes"`
}

func registerCacheWarmStatus(s *mcp.Server, c *terrapod.Client) {
	type cacheWarmStatusIn struct {
		JobID string `json:"job_id" jsonschema:"the id a warm submission returned"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_cache_warm_status",
		Description: "Report a cache-warm job's progress and its PER-ITEM outcomes. Poll this after terrapod_cache_warm_packages or terrapod_cache_warm_images until `finished` is true: a submission returns a job id rather than a result, so until then nothing is known beyond that the server accepted the list. Read the outcomes rather than the counts -- \"two failed\" over a submission of twenty is not something anyone can act on, and finding the specific gaps before a node is sealed is the entire purpose. One item failing never stops the rest, so a finished job with failures has still reported every gap rather than only the first. Per outcome, `fetched` 0 against a non-zero `files` means the item was ALREADY CACHED rather than newly pulled, which is how you tell a re-run that converged from a first run that did the work; `detail` on a successful item names a part that did not land, such as one architecture of a pulumi plugin; and `source` is where the bytes actually came from, which is not always the default upstream. A job ages out after a day and then reports as not found. Read-only; requires platform admin.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in cacheWarmStatusIn) (*mcp.CallToolResult, cacheWarmStatusOut, error) {
		job, err := c.WarmJobStatus(ctx, in.JobID)
		if err != nil {
			return errResult(err), cacheWarmStatusOut{}, nil
		}
		out := cacheWarmStatusOut{
			JobID:       job.JobID,
			Status:      job.Status,
			Finished:    job.Finished(),
			Total:       job.Total,
			Completed:   job.Completed,
			Succeeded:   job.Succeeded,
			Failed:      job.Failed,
			SubmittedAt: job.SubmittedAt,
		}
		for _, o := range job.Outcomes {
			out.Outcomes = append(out.Outcomes, cacheWarmOutcomeOut{
				Ecosystem: o.Ecosystem, Ref: o.Ref, OK: o.OK, Detail: o.Detail,
				Files: o.Files, Fetched: o.Fetched, Source: o.Source,
			})
		}
		return nil, out, nil
	})
}
