package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strconv"
)

// Admin inventory and eviction for the package cache and the VCS archive cache
// (#2474).
//
// Terrapod keeps four caches. The two binary ones have had admin routes since
// they existed; these are the other two, and they are enumerated by different
// means because they ARE different:
//
//   - the package cache (galaxy, pypi, npm, go, nuget, pulumi) has a row per
//     artifact, so listing it is a database query -- filterable, countable, and
//     paged with `page[number]` like every other collection;
//   - the VCS archive cache has NO rows. An archive is addressed entirely by
//     its storage key, so listing it walks object storage, and this SDK
//     therefore drives it with a KEY CURSOR rather than a page number. That is
//     not an inconsistency to tidy away: an offset into a paged remote listing
//     would re-walk the whole prefix for every page.
//
// Two further asymmetries a caller has to know about rather than infer:
//
//   - PACKAGE AGE IS BY LAST ACCESS; ARCHIVE AGE IS BY WRITE TIME. The package
//     table has an access column and retention reads it, because evicting an
//     artifact every run pulls merely for being old would re-fetch it
//     immediately. An archive is immutable once written and has no access
//     column, so its write time is its age -- which makes archive eviction
//     blunter: one a run pulled this morning goes if its commit is old enough.
//   - ArchiveTotalsMeta.Truncated. The archive totals come from a bounded walk,
//     so the answer can be partial, and a caller that cannot see the flag
//     cannot tell a complete total from a partial one -- in the direction that
//     understates exactly what an operator came to find.
//
// Every route here requires platform admin.
//

// ── the package cache ──────────────────────────────────────────────────────

// CachedPackageFile is one cached package artifact.
type CachedPackageFile struct {
	ID        string `json:"id"`
	Ecosystem string `json:"ecosystem"`
	Name      string `json:"name"`
	Version   string `json:"version"`
	Filename  string `json:"filename"`
	Size      int64  `json:"size"`
	// Digest is UPSTREAM's own digest, as that ecosystem expresses it -- not
	// one of Terrapod's. So it can be matched against what the registry
	// published rather than against what Terrapod stored, and its format
	// varies by ecosystem.
	Digest   string `json:"digest"`
	CachedAt string `json:"cached-at"`
	// LastAccessedAt is the column retention and age-based eviction read. It is
	// also the sort key of the listing, so the artifacts nothing has touched
	// surface last.
	LastAccessedAt string `json:"last-accessed-at"`
}

// PackageCacheListOptions filters and pages ListPackageCache.
type PackageCacheListOptions struct {
	// Ecosystem narrows to one of galaxy, pypi, npm, go, nuget, pulumi. Empty
	// means every ecosystem.
	Ecosystem string
	// Name is a PREFIX match, not an exact one: "community." returns the whole
	// collection family, and an exact name is the degenerate case of the same
	// query.
	Name string
	// PageNumber is 1-indexed (matches the server). 0 means page 1.
	PageNumber int
	// PageSize defaults to the server's default when 0.
	PageSize int
}

// PackageCacheList is one page of cached package artifacts.
type PackageCacheList struct {
	Items       []CachedPackageFile
	CurrentPage int
	PageSize    int
	TotalPages  int
	TotalCount  int
}

// PackageCacheTotal is what one ecosystem is holding.
type PackageCacheTotal struct {
	Ecosystem string `json:"ecosystem"`
	Files     int64  `json:"files"`
	Bytes     int64  `json:"bytes"`
}

// CacheTotalsMeta is the whole-cache total that accompanies the per-ecosystem
// breakdown.
//
// Aggregated server-side over the WHOLE cache, never summed from a page: a
// total computed from one page is wrong by however much the page left out, and
// wrong in the reassuring direction.
type CacheTotalsMeta struct {
	Files int64 `json:"files"`
	Bytes int64 `json:"bytes"`
	// Sealed is true when this node cannot re-fetch what it evicts
	// (`registry.cache_only`), so an eviction here is a PERMANENT deletion
	// rather than a purge. READ IT before presenting an eviction as reversible:
	// the scheduled retention sweep skips these caches for exactly this reason,
	// and the server reports the fact on the TOTALS as well as on the eviction
	// result so a caller can say so BEFORE it asks, not after.
	Sealed bool `json:"sealed"`
}

// PackageEvictRequest targets a bulk package eviction. NEITHER filter means
// the whole cache, which is a thing an operator legitimately wants after a bad
// publish -- nothing is lost either way, because every artifact here is
// re-fetchable from the registry it came from.
//
// The server forbids unknown fields, so a misspelling is a 422 rather than an
// eviction wider than the caller meant.
type PackageEvictRequest struct {
	// Ecosystem narrows to one ecosystem. Empty means all of them.
	Ecosystem string `json:"ecosystem,omitempty"`
	// OlderThanDays evicts artifacts not ACCESSED in this many days. Nil means
	// no age filter. Access-based, never write-based -- see the file comment.
	OlderThanDays *int `json:"older_than_days,omitempty"`
}

// EvictionResult is what a bulk eviction removed.
//
// One type for both caches: the question is the same one, and the endpoints
// differ only in the word they use for a unit (`files` for packages,
// `archives` for VCS archives). Both land in Files, because an archive is one
// file.
type EvictionResult struct {
	Files int64
	Bytes int64
	// Sealed is true when this node cannot re-fetch what it evicts
	// (`registry.cache_only`), so an eviction here is a PERMANENT deletion
	// rather than a purge. READ IT before presenting an eviction as reversible:
	// the scheduled retention sweep skips these caches for exactly this reason,
	// and the server reports the fact on the TOTALS as well as on the eviction
	// result so a caller can say so BEFORE it asks, not after.
	Sealed bool `json:"sealed"`
	// Warning is the server's own sentence for a sealed node, empty otherwise.
	// Carried verbatim rather than reconstructed, so one wording serves every
	// consumer and none of them has to know what `cache_only` implies.
	Warning string `json:"warning"`
}

// ListPackageCache returns one page of cached package artifacts,
// newest-ACCESSED first. Requires platform admin.
//
// The caller drives pagination explicitly so a large cache still finishes;
// ListAllPackageCache loops for you.
func (c *Client) ListPackageCache(ctx context.Context, opts PackageCacheListOptions) (*PackageCacheList, error) {
	q := url.Values{}
	if opts.Ecosystem != "" {
		q.Set("ecosystem", opts.Ecosystem)
	}
	if opts.Name != "" {
		q.Set("name", opts.Name)
	}
	if opts.PageNumber > 0 {
		q.Set("page[number]", strconv.Itoa(opts.PageNumber))
	}
	if opts.PageSize > 0 {
		q.Set("page[size]", strconv.Itoa(opts.PageSize))
	}
	path := "/api/v1/admin/package-cache"
	if encoded := q.Encode(); encoded != "" {
		path += "?" + encoded
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse package cache response: %w", err)
	}
	list := &PackageCacheList{Items: make([]CachedPackageFile, 0, len(resources))}
	for i := range resources {
		row := CachedPackageFile{ID: resources[i].ID}
		if err := decodeResourceAttributes(resources[i].Attributes, &row); err != nil {
			return nil, fmt.Errorf("decode cached package file %q: %w", resources[i].ID, err)
		}
		list.Items = append(list.Items, row)
	}
	if meta, err := parseListMeta(data); err == nil {
		list.CurrentPage = meta.CurrentPage
		list.PageSize = meta.PageSize
		list.TotalPages = meta.TotalPages
		list.TotalCount = meta.TotalCount
	}
	return list, nil
}

// ListAllPackageCache fetches every cached artifact matching opts, paging
// through the whole collection. opts.PageNumber is ignored (paging starts at
// 1); opts.PageSize sets the per-request page, defaulting to 100 here to keep
// round-trips down.
//
// Prefer ListPackageCache when you genuinely want one page -- surfacing
// total-count to a user without pulling the whole cache, say.
func (c *Client) ListAllPackageCache(ctx context.Context, opts PackageCacheListOptions) ([]CachedPackageFile, error) {
	pageSize := opts.PageSize
	if pageSize <= 0 {
		pageSize = 100
	}
	var all []CachedPackageFile
	for page := 1; ; page++ {
		list, err := c.ListPackageCache(ctx, PackageCacheListOptions{
			Ecosystem:  opts.Ecosystem,
			Name:       opts.Name,
			PageNumber: page,
			PageSize:   pageSize,
		})
		if err != nil {
			return nil, err
		}
		all = append(all, list.Items...)
		// TotalPages is authoritative; a short page is the fallback for a
		// server that returned no pagination meta.
		if list.TotalPages > 0 {
			if page >= list.TotalPages {
				break
			}
		} else if len(list.Items) < pageSize {
			break
		}
	}
	return all, nil
}

// PackageCacheTotals returns count and bytes per ecosystem, largest-first by
// the server's ordering, plus the whole-cache total. Requires platform admin.
func (c *Client) PackageCacheTotals(ctx context.Context) ([]PackageCacheTotal, CacheTotalsMeta, error) {
	data, err := c.Get(ctx, "/api/v1/admin/package-cache/totals")
	if err != nil {
		return nil, CacheTotalsMeta{}, err
	}
	var doc struct {
		Data []struct {
			Attributes PackageCacheTotal `json:"attributes"`
		} `json:"data"`
		Meta CacheTotalsMeta `json:"meta"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, CacheTotalsMeta{}, fmt.Errorf("parse package cache totals: %w", err)
	}
	totals := make([]PackageCacheTotal, 0, len(doc.Data))
	for _, d := range doc.Data {
		totals = append(totals, d.Attributes)
	}
	return totals, doc.Meta, nil
}

// DeletePackageCacheFile evicts one artifact by its row id.
//
// By id rather than by (ecosystem, name, filename): that tuple is the unique
// constraint so either would address one row, and an id cannot be mangled by a
// filename carrying a slash or a `+` the way a path segment can. Returns
// *NotFoundError when the row is already gone, which most callers treat as
// success. Requires platform admin.
func (c *Client) DeletePackageCacheFile(ctx context.Context, id string) error {
	if id == "" {
		return errors.New("cached package file id is required")
	}
	return c.Delete(ctx, "/api/v1/admin/package-cache/"+url.PathEscape(id))
}

// EvictPackageCache evicts artifacts in bulk by ecosystem and/or age, and
// reports how much went. Requires platform admin.
func (c *Client) EvictPackageCache(ctx context.Context, req PackageEvictRequest) (*EvictionResult, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal package eviction: %w", err)
	}
	data, err := c.Post(ctx, "/api/v1/admin/package-cache/evict", payload)
	if err != nil {
		return nil, err
	}
	var out struct {
		Files   int64  `json:"files"`
		Bytes   int64  `json:"bytes"`
		Sealed  bool   `json:"sealed"`
		Warning string `json:"warning"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse package eviction result: %w", err)
	}
	return &EvictionResult{
		Files: out.Files, Bytes: out.Bytes, Sealed: out.Sealed, Warning: out.Warning,
	}, nil
}

// ── the VCS archive cache ──────────────────────────────────────────────────

// CachedVCSArchive is one cached VCS archive.
//
// Every field but Size and LastModified is READ OUT OF THE KEY -- there is no
// row to read instead. So an archive whose key the server could not parse
// comes back with its Key and size and the rest empty, rather than being
// dropped: a key nothing can read is exactly the one an operator most needs to
// see, since nothing else will ever show it to them.
type CachedVCSArchive struct {
	// Key is the storage key, and it is also the id: it is what
	// DeleteVCSArchive takes.
	Key          string `json:"key"`
	ConnectionID string `json:"connection-id"`
	Owner        string `json:"owner"`
	Repo         string `json:"repo"`
	SHA          string `json:"sha"`
	// PathsHash is "full" for a whole-repo archive, otherwise the hash of the
	// path-narrowing set the workspace asked for.
	PathsHash string `json:"paths-hash"`
	Size      int64  `json:"size"`
	// LastModified is the write time, which for an immutable archive IS its
	// age. Empty when the storage backend reported none.
	LastModified string `json:"last-modified"`
}

// VCSArchiveListOptions pages ListVCSArchiveCache.
//
// There is no PageNumber, deliberately: see the file comment. Resume with the
// After cursor the previous page returned.
type VCSArchiveListOptions struct {
	// ConnectionID narrows to one VCS connection. It narrows the storage
	// PREFIX rather than filtering a page, so inspecting one connection does
	// not pay for the whole estate.
	ConnectionID string
	// After is the key cursor to resume from -- the NextCursor of the previous
	// page. Empty starts at the beginning.
	After string
	// Limit is the maximum entries for this page. 0 takes the server's default.
	Limit int
}

// VCSArchiveList is one page of cached archives.
type VCSArchiveList struct {
	Items []CachedVCSArchive
	// NextCursor is EMPTY WHEN THIS IS THE LAST PAGE -- that is how a caller
	// knows it has finished. Otherwise pass it back as
	// VCSArchiveListOptions.After.
	//
	// A final page that happens to be exactly full still carries a cursor, so
	// the request that returns nothing is the normal way such a walk ends: loop
	// on the cursor being empty, never on a page being shorter than the limit.
	// ListAllVCSArchiveCache does that for you.
	NextCursor string
	// PageSize is the server's `meta.page-size`: how many entries this page
	// carries, which equals len(Items). It reports the page produced, NOT the
	// limit that was asked for.
	PageSize int
}

// VCSArchiveTotal is what one repository's archives are holding.
type VCSArchiveTotal struct {
	ConnectionID string `json:"connection-id"`
	Owner        string `json:"owner"`
	Repo         string `json:"repo"`
	Archives     int64  `json:"archives"`
	Bytes        int64  `json:"bytes"`
}

// ArchiveTotalsMeta is the whole-cache archive total, plus whether the walk
// that produced it was cut short.
type ArchiveTotalsMeta struct {
	Archives int64 `json:"archives"`
	Bytes    int64 `json:"bytes"`
	// Truncated is true when the storage walk hit ScanLimit, so Archives and
	// Bytes are a LOWER BOUND rather than the answer. READ IT: a caller that
	// presents a truncated total as complete understates exactly what an
	// operator came here to find.
	Truncated bool `json:"truncated"`
	// ScanLimit is the ceiling the walk stopped at, so a caller can say how
	// much of the estate the number covers.
	ScanLimit int `json:"scan-limit"`
	// Sealed is true when this node cannot re-fetch what it evicts
	// (`registry.cache_only`), so an eviction here is a PERMANENT deletion
	// rather than a purge. READ IT before presenting an eviction as reversible:
	// the scheduled retention sweep skips these caches for exactly this reason,
	// and the server reports the fact on the TOTALS as well as on the eviction
	// result so a caller can say so BEFORE it asks, not after.
	Sealed bool `json:"sealed"`
}

// ArchiveEvictRequest targets a bulk archive eviction. As with packages,
// neither filter means everything.
type ArchiveEvictRequest struct {
	// ConnectionID narrows to one VCS connection. Empty means all of them.
	ConnectionID string `json:"connection_id,omitempty"`
	// OlderThanDays evicts archives WRITTEN more than this many days ago. Nil
	// means no age filter. Write-based, not access-based -- see the file
	// comment.
	OlderThanDays *int `json:"older_than_days,omitempty"`
}

// ListVCSArchiveCache returns one page of cached archives plus the cursor to
// resume from. Requires platform admin.
//
// Cursor-paged rather than offset-paged, and so carrying no pagination meta --
// see the file comment for why. Check NextCursor, not len(Items), to decide
// whether to ask for more.
func (c *Client) ListVCSArchiveCache(ctx context.Context, opts VCSArchiveListOptions) (*VCSArchiveList, error) {
	q := url.Values{}
	if opts.ConnectionID != "" {
		q.Set("connection_id", opts.ConnectionID)
	}
	if opts.After != "" {
		q.Set("after", opts.After)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	path := "/api/v1/admin/vcs-archive-cache"
	if encoded := q.Encode(); encoded != "" {
		path += "?" + encoded
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	// Decoded whole rather than through ParseResourceList + parseListMeta: the
	// cursor lives in a `meta` block that is not the pagination shape, so the
	// shared meta parser cannot read it, and the resource id here is just the
	// key again.
	var doc struct {
		Data []struct {
			Attributes CachedVCSArchive `json:"attributes"`
		} `json:"data"`
		Meta struct {
			NextCursor string `json:"next-cursor"`
			PageSize   int    `json:"page-size"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse vcs archive cache response: %w", err)
	}
	list := &VCSArchiveList{
		Items:      make([]CachedVCSArchive, 0, len(doc.Data)),
		NextCursor: doc.Meta.NextCursor,
		PageSize:   doc.Meta.PageSize,
	}
	for _, d := range doc.Data {
		list.Items = append(list.Items, d.Attributes)
	}
	return list, nil
}

// ListAllVCSArchiveCache fetches every cached archive matching opts by
// FOLLOWING THE CURSOR -- never by incrementing a page number, which this
// endpoint does not have.
//
// opts.After is ignored (it starts at the beginning); opts.Limit sets the
// per-request page, defaulting to 100 here to keep round-trips down.
//
// It stops on an EMPTY CURSOR, never on a short page: a final page that
// happens to be exactly full still carries a cursor, so the request that
// returns nothing is how such a walk normally ends. The second guard is for a
// server that hands back a cursor it has already given -- nothing else here
// bounds the loop, so without it a caller would hang rather than get an answer.
func (c *Client) ListAllVCSArchiveCache(ctx context.Context, opts VCSArchiveListOptions) ([]CachedVCSArchive, error) {
	limit := opts.Limit
	if limit <= 0 {
		limit = 100
	}
	var all []CachedVCSArchive
	cursor := ""
	for {
		page, err := c.ListVCSArchiveCache(ctx, VCSArchiveListOptions{
			ConnectionID: opts.ConnectionID,
			After:        cursor,
			Limit:        limit,
		})
		if err != nil {
			return nil, err
		}
		all = append(all, page.Items...)
		if page.NextCursor == "" || page.NextCursor == cursor {
			break
		}
		cursor = page.NextCursor
	}
	return all, nil
}

// VCSArchiveCacheTotals returns count and bytes per repository, largest first,
// plus the whole-cache total AND whether the walk was truncated. Requires
// platform admin.
//
// Always read the meta's Truncated before presenting the numbers.
func (c *Client) VCSArchiveCacheTotals(ctx context.Context) ([]VCSArchiveTotal, ArchiveTotalsMeta, error) {
	data, err := c.Get(ctx, "/api/v1/admin/vcs-archive-cache/totals")
	if err != nil {
		return nil, ArchiveTotalsMeta{}, err
	}
	var doc struct {
		Data []struct {
			Attributes VCSArchiveTotal `json:"attributes"`
		} `json:"data"`
		Meta ArchiveTotalsMeta `json:"meta"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, ArchiveTotalsMeta{}, fmt.Errorf("parse vcs archive cache totals: %w", err)
	}
	totals := make([]VCSArchiveTotal, 0, len(doc.Data))
	for _, d := range doc.Data {
		totals = append(totals, d.Attributes)
	}
	return totals, doc.Meta, nil
}

// DeleteVCSArchive evicts one archive by its storage key. Requires platform
// admin.
//
// The key travels as a QUERY parameter rather than a path segment, because it
// contains slashes. The server refuses anything not under the archive prefix
// with a 422 (IsValidation) -- this route takes a storage key from a request,
// so without that check a typo could delete a state version or a plan artifact
// through an endpoint whose name says "cache".
func (c *Client) DeleteVCSArchive(ctx context.Context, key string) error {
	if key == "" {
		return errors.New("archive key is required")
	}
	return c.Delete(ctx, "/api/v1/admin/vcs-archive-cache?key="+url.QueryEscape(key))
}

// EvictVCSArchiveCache evicts archives in bulk by connection and/or age, and
// reports how much went. The server reports the count as `archives`; it lands
// in EvictionResult.Files. Requires platform admin.
func (c *Client) EvictVCSArchiveCache(ctx context.Context, req ArchiveEvictRequest) (*EvictionResult, error) {
	payload, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal archive eviction: %w", err)
	}
	data, err := c.Post(ctx, "/api/v1/admin/vcs-archive-cache/evict", payload)
	if err != nil {
		return nil, err
	}
	var out struct {
		Archives int64  `json:"archives"`
		Bytes    int64  `json:"bytes"`
		Sealed   bool   `json:"sealed"`
		Warning  string `json:"warning"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse archive eviction result: %w", err)
	}
	return &EvictionResult{
		Files: out.Archives, Bytes: out.Bytes, Sealed: out.Sealed, Warning: out.Warning,
	}, nil
}
