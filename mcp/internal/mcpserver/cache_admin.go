package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerCacheAdmin adds the SIX tools over the package cache and the VCS
// archive cache (#2474, #2494): two reads and the four evictions. Every one is
// platform-admin.
//
// # The four evictions used to be absent, on a reason that does not hold
//
// This group shipped with the two listings only. The reason given, in this
// comment and repeated in llms.txt and docs/mcp.md, was that "an eviction's
// only result is a slower run later, for somebody who did not ask, which is an
// operator's call to make rather than an agent's". That is a property of the
// OPERATION, which is the shape AGENTS.md forbids:
//
//	a reason to leave something off a tool has to be a property of the CALLER;
//	a reason that is a property of the SETTING ("this one is consequential")
//	applies equally to every other client and is therefore an argument about
//	the feature, not about MCP.
//
// It is operation-shaped by the mechanical test: nothing in it changes when the
// caller changes. The cost lands on the same third party whether an operator
// clicks the panel's button, curls the route, or asks an agent to. So it was an
// argument for making the eviction harder to do by accident -- which is what
// the panel's confirm() and this group's `destructive` annotations do -- and
// never an argument for one client not having it.
//
// Its one caller-flavoured clause, that the action "has no observable result an
// agent can reason about", was also false on its own terms. The eviction routes
// return what they removed, in bytes, plus whether more remains and where to
// resume; the listing beside them reports the before and the after. That is
// more legible than most tools' results.
//
// MCP is not an authorization boundary and never was: this server carries the
// user's own `tofu login` token and the API applies exactly the same RBAC it
// applies to the UI, the provider and curl. So the omission protected nobody --
// an admin reached the eviction through another door and a non-admin was refused
// through every door -- and produced only the asymmetry AGENTS.md predicts: an
// agent could read that one ecosystem was holding 40 GiB and could not act on
// it, so it described a change it could not make.
//
// # Risk is carried by the annotation, not by the absence
//
// All four evictions are `destructive`, so a host asks the user before running
// one -- the same treatment terrapod_policy_check_override and
// terrapod_run_security_scan_override get, both of which defeat a MANDATORY
// gate and are therefore considerably more consequential than making a cache
// miss. The thing genuinely worth saying to an agent -- that the cost arrives
// later and lands on somebody else, and that on a sealed node there is nothing
// to restore from -- is said in each description, where an agent reads it,
// rather than expressed by a tool it cannot see.
//
// # Each listing carries its own totals
//
// One tool rather than two per cache, because a listing and its total answer
// one question -- "what is in here and how much of it" -- and a separate totals
// tool is a round-trip an agent has to know to make. The totals are computed
// server-side over the WHOLE cache, never summed from the page, so they do not
// understate when the page is short.
func registerCacheAdmin(s *mcp.Server, c *terrapod.Client) {
	registerPackageCacheRead(s, c)
	registerVCSArchiveCacheRead(s, c)
	registerPackageCacheEvict(s, c)
	registerVCSArchiveCacheEvict(s, c)
}

// ── terrapod_package_cache_list ────────────────────────────────────────────

type packageCacheFileOut struct {
	ID        string `json:"id"`
	Ecosystem string `json:"ecosystem"`
	Name      string `json:"name"`
	Version   string `json:"version,omitempty"`
	Filename  string `json:"filename,omitempty"`
	Size      int64  `json:"size"`
	Digest    string `json:"digest,omitempty"`
	CachedAt  string `json:"cached_at,omitempty"`
	// LastAccessedAt is the field age-based eviction reads and the sort key of
	// the listing, so it is what an operator deciding what to evict looks at.
	LastAccessedAt string `json:"last_accessed_at,omitempty"`
}

type packageCacheTotalOut struct {
	Ecosystem string `json:"ecosystem"`
	Files     int64  `json:"files"`
	Bytes     int64  `json:"bytes"`
}

type packageCacheTotalsOut struct {
	ByEcosystem []packageCacheTotalOut `json:"by_ecosystem"`
	Files       int64                  `json:"files"`
	Bytes       int64                  `json:"bytes"`
	// Sealed true means this node cannot re-fetch what it evicts
	// (`registry.cache_only`), so clearing anything here is PERMANENT. Say so
	// before recommending an eviction: the scheduled retention sweep skips
	// these caches for exactly that reason.
	Sealed bool `json:"sealed"`
}

func registerPackageCacheRead(s *mcp.Server, c *terrapod.Client) {
	type packageCacheListIn struct {
		Ecosystem  string `json:"ecosystem,omitempty" jsonschema:"return only one ecosystem's artifacts: galaxy, pypi, npm, go, nuget or pulumi. Omit for all of them"`
		Name       string `json:"name,omitempty" jsonschema:"filter by package name as a PREFIX, not an exact match: community. returns the whole collection family, and an exact name is the degenerate case of the same query"`
		PageSize   int    `json:"page_size,omitempty" jsonschema:"max artifacts to return in this page (default 50). The totals always cover the whole cache regardless"`
		PageNumber int    `json:"page_number,omitempty" jsonschema:"1-indexed page to return (default 1)"`
	}
	type packageCacheListOut struct {
		// Returned is this page; Total is every artifact matching the filters.
		// Returned < Total means the page was truncated.
		Returned  int                   `json:"returned"`
		Total     int                   `json:"total"`
		Truncated bool                  `json:"truncated"`
		Files     []packageCacheFileOut `json:"files"`
		// Totals cover the WHOLE cache and are unaffected by the filters or
		// the page, so they can legitimately be larger than the page.
		Totals packageCacheTotalsOut `json:"totals"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_package_cache_list",
		Description: "List what Terrapod has cached from the package ecosystems its runs pull through -- galaxy (Ansible collections and roles), pypi, npm, go, nuget and pulumi -- with per-ecosystem and whole-cache totals. This is the tool for \"why is the configure slow\" and \"what is filling the object store\": a cache miss means the run fetches from the upstream registry instead, so an artifact's ABSENCE here is the finding, and `size` against `bytes` is where the storage went. Each artifact reports `digest`, which is UPSTREAM's own digest as that ecosystem expresses it rather than one of Terrapod's, so it can be matched against what the registry published. Read `last_accessed_at` rather than `cached_at` when judging what is dead weight: the listing is sorted newest-accessed first and that field is the one age-based eviction uses, because an artifact every run pulls would otherwise be evicted merely for being old and re-fetched immediately. `totals` are aggregated server-side over the whole cache and are NOT filtered by `ecosystem`/`name` or limited by the page, so they stay correct when the page is short. THIS TOOL ONLY READS, and the eviction is a tool too: `terrapod_package_cache_delete` removes one artifact and `terrapod_package_cache_evict` removes an ecosystem or everything past an age. Both are annotated destructive so a host asks first, which is where that question belongs -- the cost of an eviction is a slower run minutes or hours later for somebody who did not ask, so say what you found and what you propose rather than purging on your own initiative. Nothing is corrupted by one, because every artifact here is re-fetchable from the registry it came from -- EXCEPT when `totals.sealed` is true, which means this node cannot re-fetch anything and an eviction is a permanent deletion. Read `totals.sealed` before recommending one. Read-only; requires platform admin.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in packageCacheListIn) (*mcp.CallToolResult, packageCacheListOut, error) {
		size := in.PageSize
		if size <= 0 {
			size = 50
		}
		list, err := c.ListPackageCache(ctx, terrapod.PackageCacheListOptions{
			Ecosystem:  in.Ecosystem,
			Name:       in.Name,
			PageSize:   size,
			PageNumber: in.PageNumber,
		})
		if err != nil {
			return errResult(err), packageCacheListOut{}, nil
		}
		totals, meta, err := c.PackageCacheTotals(ctx)
		if err != nil {
			return errResult(err), packageCacheListOut{}, nil
		}

		// max() guards a server that returned no pagination meta: reporting a
		// total smaller than the page would read as a truncation that isn't.
		total := max(list.TotalCount, len(list.Items))
		out := packageCacheListOut{
			Returned:  len(list.Items),
			Total:     total,
			Truncated: len(list.Items) < total,
			Totals: packageCacheTotalsOut{
				Files:  meta.Files,
				Bytes:  meta.Bytes,
				Sealed: meta.Sealed,
			},
		}
		for i := range list.Items {
			f := &list.Items[i]
			out.Files = append(out.Files, packageCacheFileOut{
				ID: f.ID, Ecosystem: f.Ecosystem, Name: f.Name, Version: f.Version,
				Filename: f.Filename, Size: f.Size, Digest: f.Digest,
				CachedAt: f.CachedAt, LastAccessedAt: f.LastAccessedAt,
			})
		}
		for _, t := range totals {
			out.Totals.ByEcosystem = append(out.Totals.ByEcosystem, packageCacheTotalOut{
				Ecosystem: t.Ecosystem, Files: t.Files, Bytes: t.Bytes,
			})
		}
		return nil, out, nil
	})
}

// ── terrapod_vcs_archive_cache_list ───────────────────────────────────────

type vcsArchiveOut struct {
	// Key is the storage key and the archive's identity. Every field below it
	// but Size and LastModified is read out of the key, so an archive whose
	// key the server could not parse arrives with only Key and Size filled --
	// surfaced rather than dropped, because a key nothing can read is the one
	// an operator most needs to see.
	Key          string `json:"key"`
	ConnectionID string `json:"connection_id,omitempty"`
	Owner        string `json:"owner,omitempty"`
	Repo         string `json:"repo,omitempty"`
	SHA          string `json:"sha,omitempty"`
	// PathsHash is "full" for a whole-repo archive, otherwise the hash of the
	// path-narrowing set the workspace asked for.
	PathsHash string `json:"paths_hash,omitempty"`
	Size      int64  `json:"size"`
	// LastModified is the write time, which for an immutable archive IS its
	// age -- there is no access time to prefer.
	LastModified string `json:"last_modified,omitempty"`
}

type vcsArchiveTotalOut struct {
	ConnectionID string `json:"connection_id,omitempty"`
	Owner        string `json:"owner,omitempty"`
	Repo         string `json:"repo,omitempty"`
	Archives     int64  `json:"archives"`
	Bytes        int64  `json:"bytes"`
}

type vcsArchiveTotalsOut struct {
	ByRepo   []vcsArchiveTotalOut `json:"by_repo"`
	Archives int64                `json:"archives"`
	Bytes    int64                `json:"bytes"`
	// Truncated true means Archives and Bytes are a LOWER BOUND: the storage
	// walk hit ScanLimit and whatever lay past it is not counted. It is
	// surfaced because a truncated total presented as complete understates
	// exactly what the caller came to find.
	Truncated bool `json:"truncated"`
	ScanLimit int  `json:"scan_limit"`
	// Sealed true means this node cannot re-fetch what it evicts
	// (`registry.cache_only`), so clearing anything here is PERMANENT. Say so
	// before recommending an eviction: the scheduled retention sweep skips
	// these caches for exactly that reason.
	Sealed bool `json:"sealed"`
}

func registerVCSArchiveCacheRead(s *mcp.Server, c *terrapod.Client) {
	type vcsArchiveCacheListIn struct {
		ConnectionID string `json:"connection_id,omitempty" jsonschema:"return only one VCS connection's archives. This narrows the storage walk itself rather than filtering a page, so prefer it over reading everything"`
		After        string `json:"after,omitempty" jsonschema:"the key cursor to resume from: pass back the next_cursor of the previous call. Omit to start at the beginning. There is no page number -- this listing walks object storage, which is ordered by key"`
		Limit        int    `json:"limit,omitempty" jsonschema:"max archives to return in this page (default 100)"`
	}
	type vcsArchiveCacheListOut struct {
		Returned int `json:"returned"`
		// NextCursor empty means this was the last page. A final page that
		// happens to be exactly full still carries a cursor, so the request
		// that returns nothing is how the walk normally ends -- never treat a
		// short page as the end.
		NextCursor string          `json:"next_cursor"`
		Archives   []vcsArchiveOut `json:"archives"`
		// Totals is null when resuming a cursor (`after` was given), because
		// computing it walks storage and the answer would be the same on every
		// page. Null means NOT COMPUTED, never "the cache is empty".
		//
		// It covers the WHOLE cache and is NOT narrowed by ConnectionID: the
		// server's totals route takes no filter. So on a narrowed listing the
		// top-level counts belong to the estate rather than to the connection,
		// and the per-connection answer is the matching ByRepo rows.
		Totals *vcsArchiveTotalsOut `json:"totals"`
	}

	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_vcs_archive_cache_list",
		Description: "List the repository tarballs Terrapod has cached from VCS, with per-repository and whole-cache totals. Each entry is one commit's archive for one path-narrowing set, so `sha` plus `paths_hash` identify it and `paths_hash` is \"full\" for a whole-repo archive. Use it to find where VCS cache storage has gone (`totals.by_repo`, largest first) and to see whether a repository a run keeps re-fetching is cached at all. TWO THINGS HERE BEHAVE UNLIKE THE OTHER LISTINGS AND READING THEM WRONG GIVES A CONFIDENTLY WRONG ANSWER. First, paging is a KEY CURSOR, not a page number: there are no database rows behind this, so a page comes from a walk of object storage, and you resume by passing the previous call's `next_cursor` back as `after`. An EMPTY `next_cursor` is the only end-of-listing signal, and a page shorter than your `limit` always carries one -- so a short page IS the end and you may stop there. The case to know about is the other one: a final page that happens to be exactly full still carries a cursor, and the request that comes back with no entries is the normal way that walk ends. Second, `totals.truncated` true means the walk hit `totals.scan_limit` and the counts are a LOWER BOUND rather than the answer, so say so rather than reporting the number as the size of the cache. `totals` is null when you pass `after`, because it would be the same on every page -- null means it was not computed on this call and never that the cache is empty. Third, `totals` always covers the WHOLE cache and is NOT narrowed by `connection_id`, so on a narrowed listing the top-level `archives` and `bytes` belong to the estate rather than to that connection -- read the matching `totals.by_repo` rows for the connection's own share, and do not attribute the estate's number to it. An archive's age is its `last_modified`: it is immutable once written, so unlike a package artifact there is no last-access time. THIS TOOL ONLY READS, and the eviction is a tool too: `terrapod_vcs_archive_delete` removes one archive by key and `terrapod_vcs_archive_cache_evict` removes a connection's worth or everything past an age. Both are annotated destructive so a host asks first, which is where that question belongs -- the cost of an eviction is a slower run later for somebody who did not ask, so say what you found and what you propose rather than purging on your own initiative. Nothing is corrupted by one, because an archive is re-fetched from VCS on the next run -- EXCEPT when `totals.sealed` is true, which means this node cannot re-fetch anything and an eviction is a permanent deletion. Read-only; requires platform admin.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in vcsArchiveCacheListIn) (*mcp.CallToolResult, vcsArchiveCacheListOut, error) {
		page, err := c.ListVCSArchiveCache(ctx, terrapod.VCSArchiveListOptions{
			ConnectionID: in.ConnectionID,
			After:        in.After,
			Limit:        in.Limit,
		})
		if err != nil {
			return errResult(err), vcsArchiveCacheListOut{}, nil
		}
		out := vcsArchiveCacheListOut{
			Returned:   len(page.Items),
			NextCursor: page.NextCursor,
		}
		for i := range page.Items {
			a := &page.Items[i]
			out.Archives = append(out.Archives, vcsArchiveOut{
				Key: a.Key, ConnectionID: a.ConnectionID, Owner: a.Owner, Repo: a.Repo,
				SHA: a.SHA, PathsHash: a.PathsHash, Size: a.Size, LastModified: a.LastModified,
			})
		}

		// The totals come from a bounded storage walk, so they are fetched on
		// the FIRST page only: an agent paging a large estate would otherwise
		// pay for the same walk on every page to be told the same number.
		// Resuming a cursor is by definition not the orientation read the
		// totals answer.
		if in.After != "" {
			return nil, out, nil
		}
		totals, meta, err := c.VCSArchiveCacheTotals(ctx)
		if err != nil {
			return errResult(err), vcsArchiveCacheListOut{}, nil
		}
		out.Totals = &vcsArchiveTotalsOut{
			Archives:  meta.Archives,
			Bytes:     meta.Bytes,
			Truncated: meta.Truncated,
			ScanLimit: meta.ScanLimit,
			Sealed:    meta.Sealed,
		}
		for _, t := range totals {
			out.Totals.ByRepo = append(out.Totals.ByRepo, vcsArchiveTotalOut{
				ConnectionID: t.ConnectionID, Owner: t.Owner, Repo: t.Repo,
				Archives: t.Archives, Bytes: t.Bytes,
			})
		}
		return nil, out, nil
	})
}

// ── the four evictions ─────────────────────────────────────────────────────
//
// See registerCacheAdmin for why these exist and why the reason they did not is
// withdrawn. Every one is `destructive`, so a host asks the user first.
//
// What a description here has to carry, because the annotation cannot say it:
//
//   - the cost arrives LATER and lands on somebody else -- a slower next run.
//     So the agent's job is to report and propose, not to purge on its own
//     initiative. That was the true half of the old omission reason and it
//     belongs here, where an agent reads it;
//   - on a SEALED node (`registry.cache_only`) an eviction is a permanent
//     deletion, because nothing can re-fetch what went. The listings report
//     `totals.sealed` so it can be read BEFORE asking, and the eviction result
//     repeats it with the server's own sentence;
//   - a bulk eviction is BOUNDED per call and `truncated` says when more
//     remains. Reporting a bounded sweep as a finished one is the defect these
//     fields exist to remove (#2491).

// cacheEvictOut is what a bulk eviction removed, for either cache.
//
// One type for both, because the question is the same one -- the routes differ
// only in the word they use for a unit, and both land in Files for the same
// reason the SDK's EvictionResult does: an archive is one file.
type cacheEvictOut struct {
	Files int64 `json:"files"`
	Bytes int64 `json:"bytes"`
	// Truncated true means MORE MATCHED THAN THIS CALL REMOVED. Say so rather
	// than reporting the count as the whole purge: the operator reloads the
	// totals, sees a size that has barely moved, and reads it as the eviction
	// having done nothing.
	Truncated bool `json:"truncated"`
	// NextCursor is where to resume an ARCHIVE sweep -- pass it back as
	// `after` with every other field unchanged. Always empty for the package
	// cache, which resumes by itself because the rows that matched are the rows
	// that went.
	NextCursor string `json:"next_cursor,omitempty"`
	// Sealed true means this node could not re-fetch what just went, so the
	// eviction was a PERMANENT DELETION. Warning is the server's own sentence
	// for it, carried verbatim.
	Sealed  bool   `json:"sealed"`
	Warning string `json:"warning,omitempty"`
}

// cacheDeleteOut is what a single-artifact eviction removed.
type cacheDeleteOut struct {
	Deleted bool   `json:"deleted"`
	ID      string `json:"id"`
}

func registerPackageCacheEvict(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_package_cache_delete ──────────────────────────────────
	type packageCacheDeleteIn struct {
		FileID string `json:"file_id" jsonschema:"the artifact's row id, as terrapod_package_cache_list reports it as its id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_package_cache_delete",
		Description: "Evict ONE cached package artifact by its row id (the `id` terrapod_package_cache_list reports). Use it for a single bad artifact -- a tampered or truncated download, a version withdrawn upstream -- rather than to reclaim space, which is what terrapod_package_cache_evict is for. The artifact is re-fetched from the registry it came from on the next run that wants it, so this costs a slower run and no data, which is what makes this cache evictable at all. THE ONE EXCEPTION, AND READ FOR IT FIRST: on a sealed node (`registry.cache_only`, reported as `totals.sealed` by the listing) nothing can be re-fetched and this is a permanent deletion with nothing able to restore it. The cost of any eviction also arrives later and lands on whoever runs next, not on whoever asked for it -- so say what you found and what you propose rather than purging on your own initiative. A row that is already gone is a 404. Destructive; requires platform admin.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in packageCacheDeleteIn) (*mcp.CallToolResult, *cacheDeleteOut, error) {
		if in.FileID == "" {
			return errText("file_id is required"), nil, nil
		}
		if err := c.DeletePackageCacheFile(ctx, in.FileID); err != nil {
			return errResult(err), nil, nil
		}
		return nil, &cacheDeleteOut{Deleted: true, ID: in.FileID}, nil
	})

	// ── terrapod_package_cache_evict ───────────────────────────────────
	type packageCacheEvictIn struct {
		Ecosystem     string `json:"ecosystem,omitempty" jsonschema:"evict only this ecosystem: galaxy, pypi, npm, go, nuget or pulumi. Omit for EVERY ecosystem -- neither filter means the whole cache, which is deliberate and is a thing an operator legitimately wants after a bad publish"`
		OlderThanDays *int   `json:"older_than_days,omitempty" jsonschema:"evict artifacts not ACCESSED in this many days. Omit for no age filter. Access-based rather than write-based, because an artifact every run pulls would otherwise be evicted merely for being old and re-fetched immediately. ZERO MEANS EVERYTHING, not disabled -- unlike the retention config keys, where 0 switches a category off"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_package_cache_evict",
		Description: "Evict cached package artifacts IN BULK, by ecosystem and/or by how long since they were last accessed. This is the tool for reclaiming space after terrapod_package_cache_list has shown where it went, and for clearing an ecosystem after a bad publish. OMITTING BOTH FILTERS EVICTS THE WHOLE CACHE, which is deliberate rather than a missing guard, so never pass neither by accident. `older_than_days: 0` means EVERYTHING past zero days -- that is, everything -- and not \"disabled\", which is what 0 means in the retention config keys. Age is measured from LAST ACCESS, not from when the artifact was cached, so a package nothing has read for a month is evictable however recently it was published. THE CALL IS BOUNDED: it removes at most a thousand artifacts, and `truncated` true means more matched than it took. Call it again with the same filters to continue -- there is no cursor to carry, because the artifacts that matched are the ones that went. Report `truncated` rather than presenting the count as a finished purge: an operator who reloads the totals and sees barely any change reads that as the eviction having done nothing. Nothing is lost by an eviction here, because every artifact is re-fetchable from its registry -- EXCEPT on a sealed node, where `sealed` comes back true and `warning` carries the server's own sentence: there the eviction was a permanent deletion. Read `totals.sealed` from the listing BEFORE recommending one, not after. The cost of an eviction arrives later and lands on whoever runs next rather than on whoever asked, so say what you found and what you propose and let the operator decide. Destructive; requires platform admin.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in packageCacheEvictIn) (*mcp.CallToolResult, *cacheEvictOut, error) {
		res, err := c.EvictPackageCache(ctx, terrapod.PackageEvictRequest{
			Ecosystem:     in.Ecosystem,
			OlderThanDays: in.OlderThanDays,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, &cacheEvictOut{
			Files: res.Files, Bytes: res.Bytes, Truncated: res.Truncated,
			// NextCursor is deliberately not carried: it is always empty for
			// this cache, and surfacing an always-empty field would invite an
			// agent to read the absence of a cursor as the end of the sweep
			// when `truncated` is what says so.
			Sealed: res.Sealed, Warning: res.Warning,
		}, nil
	})
}

func registerVCSArchiveCacheEvict(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_vcs_archive_delete ────────────────────────────────────
	type vcsArchiveDeleteIn struct {
		Key string `json:"key" jsonschema:"the archive's storage key, as terrapod_vcs_archive_cache_list reports it as its key. It IS the archive's identity -- there is no row and no other id. Refused with a 422 unless it sits under vcs_archives/, because this takes a storage key from a request and without that check a typo could delete a state version or a plan artifact"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_vcs_archive_delete",
		Description: "Evict ONE cached VCS archive by its storage key (the `key` terrapod_vcs_archive_cache_list reports, which is also its identity -- there is no row behind it). Use it for a single bad or stale tarball rather than to reclaim space, which is terrapod_vcs_archive_cache_evict's job. The next poll, plan or drift check that wants that commit and path set re-fetches it from VCS, so this costs one slower operation and no data. A key not under `vcs_archives/` is refused with a 422 -- the route takes a storage key from a request, so without that check a typo could delete a state version or a plan artifact through an endpoint whose name says cache. THE DELETE IS IDEMPOTENT: a well-formed key that is already gone reports the same success, because there is no row for a 404 to be about -- unlike terrapod_package_cache_delete, which does 404. On a sealed node (`registry.cache_only`, reported as `totals.sealed` by the listing) nothing can be re-fetched and this is a permanent deletion. The cost of any eviction arrives later and lands on whoever runs next rather than on whoever asked, so report and propose rather than purging on your own initiative. Destructive; requires platform admin.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in vcsArchiveDeleteIn) (*mcp.CallToolResult, *cacheDeleteOut, error) {
		if in.Key == "" {
			return errText("key is required"), nil, nil
		}
		if err := c.DeleteVCSArchive(ctx, in.Key); err != nil {
			return errResult(err), nil, nil
		}
		return nil, &cacheDeleteOut{Deleted: true, ID: in.Key}, nil
	})

	// ── terrapod_vcs_archive_cache_evict ───────────────────────────────
	type vcsArchiveCacheEvictIn struct {
		ConnectionID  string `json:"connection_id,omitempty" jsonschema:"evict only this VCS connection's archives. Omit for EVERY connection -- neither filter means the whole cache, which is deliberate. It narrows the storage walk itself rather than filtering a window, so prefer it when you know which connection is the problem"`
		OlderThanDays *int   `json:"older_than_days,omitempty" jsonschema:"evict archives WRITTEN more than this many days ago. Omit for no age filter. Write-based, not access-based: an archive is immutable once written so its write time IS its age, and there is no access column to prefer. That makes this blunter than the package eviction -- an archive a run pulled this morning goes if its commit is old enough. ZERO MEANS EVERYTHING, not disabled"`
		After         string `json:"after,omitempty" jsonschema:"resume a bounded sweep from this key: pass back the next_cursor of the previous call, with every other field UNCHANGED. Omit to start at the beginning. Changing a filter while resuming makes the second window a different question from the first"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_vcs_archive_cache_evict",
		Description: "Evict cached VCS repository tarballs IN BULK, by connection and/or by how long ago they were written. This is the tool for reclaiming space after terrapod_vcs_archive_cache_list has shown which repository is holding it. OMITTING BOTH FILTERS EVICTS THE WHOLE CACHE, deliberately, so never pass neither by accident; `older_than_days: 0` means everything rather than \"disabled\". Age is the archive's WRITE time, because an archive is immutable once written and has no access time -- so this is blunter than the package eviction and one a run pulled this morning goes if its commit is old enough. THE PART THAT GIVES A WRONG ANSWER IF YOU IGNORE IT: this walks object storage and ONE CALL CONSIDERS A BOUNDED WINDOW OF KEYS, not the whole estate. `truncated` true means the window was full and more keys lie past it, and `next_cursor` is where to resume -- call again with `after` set to it and every other field unchanged, and keep going while `truncated` is true. Repeating the call WITHOUT the cursor re-examines the same window, which on a cache larger than one window purges nothing new however many times you run it; with an age filter matching nothing in that window it never advances at all. So a sweep that reports `truncated` and is not resumed has left the rest of the estate untouched, and reporting its count as a finished purge is wrong in the direction that reads as the eviction having done nothing. Nothing is lost by an eviction here, because the next run re-fetches from VCS -- EXCEPT on a sealed node, where `sealed` comes back true and `warning` carries the server's own sentence: there it was a permanent deletion. Read `totals.sealed` from the listing BEFORE recommending one. The cost arrives later and lands on whoever runs next rather than on whoever asked, so say what you found and what you propose and let the operator decide. Destructive; requires platform admin.",
		Annotations: destructive,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in vcsArchiveCacheEvictIn) (*mcp.CallToolResult, *cacheEvictOut, error) {
		res, err := c.EvictVCSArchiveCache(ctx, terrapod.ArchiveEvictRequest{
			ConnectionID:  in.ConnectionID,
			OlderThanDays: in.OlderThanDays,
			After:         in.After,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, &cacheEvictOut{
			Files: res.Files, Bytes: res.Bytes, Truncated: res.Truncated,
			NextCursor: res.NextCursor, Sealed: res.Sealed, Warning: res.Warning,
		}, nil
	})
}
