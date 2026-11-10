package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerCacheAdmin adds the two READ tools over the package cache and the
// VCS archive cache (#2474). Both are platform-admin reads.
//
// # The evict and delete endpoints exist and are deliberately NOT tools
//
// The server side of #2474 ships eight routes: list, totals, delete-one and
// bulk-evict for each cache. Only the two listings are here, and the reason is
// not that MCP is an authorization boundary -- it is not. This server carries
// the user's own `tofu login` token and the API applies exactly the same RBAC
// it applies to the UI, the provider and curl, so withholding a route from a
// tool protects nobody: an admin reaches it through another door and a
// non-admin is refused through every door.
//
// The reason is that **an eviction is an operator action with no reader on the
// other side.** What it produces is a slower next run, minutes or hours later,
// for somebody who did not ask -- a cost that lands on a third party and shows
// up nowhere an agent is looking. Nothing is corrupted (every artifact here is
// re-fetchable, which is what makes these caches evictable at all), so it is
// not a safety question; it is that the action has no observable result an
// agent can reason about, and so no business being an autonomous one.
//
// The READS are the half that earns a tool. "The configure is slow" and "what
// is filling the bucket" are questions an agent diagnosing a deployment
// actually has, and the answer is in these two listings. An agent that finds a
// cache has grown absurdly should SAY SO and name the eviction an operator can
// run, which is a better outcome than running it.
//
// # Each listing carries its own totals
//
// One tool rather than four, because a listing and its total answer one
// question -- "what is in here and how much of it" -- and a separate totals
// tool is a round-trip an agent has to know to make. The totals are computed
// server-side over the WHOLE cache, never summed from the page, so they do not
// understate when the page is short.
func registerCacheAdmin(s *mcp.Server, c *terrapod.Client) {
	registerPackageCacheRead(s, c)
	registerVCSArchiveCacheRead(s, c)
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
		Description: "List what Terrapod has cached from the package ecosystems its runs pull through -- galaxy (Ansible collections and roles), pypi, npm, go, nuget and pulumi -- with per-ecosystem and whole-cache totals. This is the tool for \"why is the configure slow\" and \"what is filling the object store\": a cache miss means the run fetches from the upstream registry instead, so an artifact's ABSENCE here is the finding, and `size` against `bytes` is where the storage went. Each artifact reports `digest`, which is UPSTREAM's own digest as that ecosystem expresses it rather than one of Terrapod's, so it can be matched against what the registry published. Read `last_accessed_at` rather than `cached_at` when judging what is dead weight: the listing is sorted newest-accessed first and that field is the one age-based eviction uses, because an artifact every run pulls would otherwise be evicted merely for being old and re-fetched immediately. `totals` are aggregated server-side over the whole cache and are NOT filtered by `ecosystem`/`name` or limited by the page, so they stay correct when the page is short. THIS TOOL ONLY READS: Terrapod also has endpoints that evict one artifact or a whole ecosystem, and they are deliberately not offered as tools -- an eviction's only result is a slower run later, for somebody who did not ask, which is an operator's call to make rather than an agent's. If a cache has grown unreasonably, report it and name the eviction an operator can run. Read-only; requires platform admin.",
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
		Description: "List the repository tarballs Terrapod has cached from VCS, with per-repository and whole-cache totals. Each entry is one commit's archive for one path-narrowing set, so `sha` plus `paths_hash` identify it and `paths_hash` is \"full\" for a whole-repo archive. Use it to find where VCS cache storage has gone (`totals.by_repo`, largest first) and to see whether a repository a run keeps re-fetching is cached at all. TWO THINGS HERE BEHAVE UNLIKE THE OTHER LISTINGS AND READING THEM WRONG GIVES A CONFIDENTLY WRONG ANSWER. First, paging is a KEY CURSOR, not a page number: there are no database rows behind this, so a page comes from a walk of object storage, and you resume by passing the previous call's `next_cursor` back as `after`. An EMPTY `next_cursor` is the only end-of-listing signal, and a page shorter than your `limit` always carries one -- so a short page IS the end and you may stop there. The case to know about is the other one: a final page that happens to be exactly full still carries a cursor, and the request that comes back with no entries is the normal way that walk ends. Second, `totals.truncated` true means the walk hit `totals.scan_limit` and the counts are a LOWER BOUND rather than the answer, so say so rather than reporting the number as the size of the cache. `totals` is null when you pass `after`, because it would be the same on every page -- null means it was not computed on this call and never that the cache is empty. Third, `totals` always covers the WHOLE cache and is NOT narrowed by `connection_id`, so on a narrowed listing the top-level `archives` and `bytes` belong to the estate rather than to that connection -- read the matching `totals.by_repo` rows for the connection's own share, and do not attribute the estate's number to it. An archive's age is its `last_modified`: it is immutable once written, so unlike a package artifact there is no last-access time. THIS TOOL ONLY READS: Terrapod can also evict one archive or a whole connection's worth, and those are deliberately not offered as tools -- the only result of an eviction is a slower run later for somebody who did not ask, so it is an operator's call. Report what you found and name the eviction instead. Read-only; requires platform admin.",
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
