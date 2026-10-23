package terrapod

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// Per-host results for a configure run (#1928).
//
// Ansible's unit of outcome is the HOST, not the resource, so a configure's
// result is a set of these rather than one status. Two reads, because they
// answer different questions and are sized differently: the summary is bounded
// by the inventory and is what a list view shows, while the events are the
// failures somebody drills into.
//
// # Paths use the canonical /api/v1 prefix with no legacy fallback
//
// These endpoints exist on no release, so a fallback to /api/terrapod/v1 is
// code that can never succeed.
//
// # Two things these types record that a reader must not re-derive
//
// Reachability is not liveness. A host that is genuinely dead reports `ok` for
// any task that does not need the connection -- a controller-side `debug`
// never attempts one -- so Status must not be presented as a reachability
// check. SawUnreachable is the honest signal and it comes from the unreachable
// EVENT rather than from Unreachable: with `ignore_unreachable: true` ansible
// reports `unreachable=0 ignored=1` while the unreachable hook fires, measured
// on ansible-core 2.18.3.
//
// An absent host is not a successful one. Under `serial:` with
// `any_errors_fatal:` ansible reports only the batch that ran, so hosts that
// were in scope appear in no event and in no recap. The server fills those from
// the run's resolved-inventory artifact and marks them Reached=false with nil
// counters -- nil rather than zero, because zero is a measurement and this is
// the absence of one. A consumer that treats a missing host as healthy is
// wrong in the one direction that matters.

// RunHostResult is one host's summary of a configure run.
type RunHostResult struct {
	ID   string `json:"id"`
	Host string `json:"host"`
	// Status is the server's derivation: unreachable, failed, ignored-failure,
	// changed or ok for a reported host, and not-reached for one the run
	// targeted but ansible never mentioned.
	Status string `json:"status"`

	// Nil on a not-reached host. See the package comment: nil is the absence of
	// a measurement rather than a zero one.
	OK          *int `json:"ok"`
	Changed     *int `json:"changed"`
	Failures    *int `json:"failures"`
	Unreachable *int `json:"unreachable"`
	Skipped     *int `json:"skipped"`
	Rescued     *int `json:"rescued"`
	Ignored     *int `json:"ignored"`

	// SawUnreachable is true when an unreachable event was recorded for this
	// host, whatever Unreachable says.
	SawUnreachable bool `json:"saw-unreachable"`
	// Reached is false for a host taken from the run's inventory rather than
	// from what ansible reported.
	Reached   bool   `json:"reached"`
	CreatedAt string `json:"created-at,omitempty"`
	UpdatedAt string `json:"updated-at,omitempty"`
}

// RunHostEvent is one failed or unreachable task event. Those two kinds and no
// others reach the database; everything ansible reported is in the run's event
// stream artifact. So an empty list means nothing failed, not nothing happened.
type RunHostEvent struct {
	ID   string `json:"id"`
	Host string `json:"host"`
	// Kind is failed or unreachable.
	Kind       string `json:"kind"`
	TaskName   string `json:"task-name"`
	TaskAction string `json:"task-action"`
	// TaskUUID correlates events within ONE run and nothing across runs:
	// ansible generates a fresh uuid per run of the same playbook (measured).
	// TaskName is the cross-run key.
	TaskUUID string `json:"task-uuid"`
	PlayName string `json:"play-name"`
	// Ignored is true when `ignore_errors` let the run carry on. The summary
	// counts such a failure under Ignored rather than Failures.
	Ignored bool   `json:"ignored"`
	Message string `json:"message"`
	// PayloadRef is nil unless the result was too large to keep inline, in
	// which case it identifies the payload in the run's oversized-payload
	// sidecar object.
	PayloadRef *int `json:"payload-ref"`
	// Sequence is the callback plugin's own counter. Order by it rather than by
	// CreatedAt, which ties when several hosts complete inside one millisecond
	// under forks.
	Sequence  int    `json:"sequence"`
	CreatedAt string `json:"created-at,omitempty"`
}

// decodeResourceAttributes decodes one list entry's attributes into out.
//
// `decodeAttributes` next door takes a whole single-resource document; these
// come from a list, where `ParseResourceList` has already split each entry's
// attributes into a map. Re-marshalling that map is one line and keeps the
// kebab-case tags on the structs above as the single description of the wire
// shape -- a hand-written field-by-field mapping is where the `any` -> float64
// trap for every numeric field comes from.
func decodeResourceAttributes(attrs map[string]json.RawMessage, out any) error {
	if len(attrs) == 0 {
		return errors.New("resource has no attributes")
	}
	raw, err := json.Marshal(attrs)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, out)
}

func runPath(runID string) (string, error) {
	if runID == "" {
		return "", errors.New("run id is required")
	}
	id := runID
	// `strings.HasPrefix` rather than the `len(id) > 4 && id[:4] != "run-"`
	// idiom the older methods here use: that guard leaves a raw id of four
	// characters or fewer UNPREFIXED, so the request goes to `/runs/abc` and
	// 404s. Harmless in practice -- every real run id is a uuid -- but it is a
	// bug rather than a deliberate bound, and a test asserting the path caught
	// it on the first run.
	if !strings.HasPrefix(id, "run-") {
		id = "run-" + id
	}
	return "/api/v1/runs/" + url.PathEscape(id), nil
}

// ListRunHostResults returns every host the run reported on PLUS the hosts it
// targeted and never reached. Requires read on the workspace.
func (c *Client) ListRunHostResults(ctx context.Context, runID string) ([]*RunHostResult, error) {
	base, err := runPath(runID)
	if err != nil {
		return nil, err
	}
	data, err := c.Get(ctx, base+"/host-results")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse host results response: %w", err)
	}
	out := make([]*RunHostResult, 0, len(resources))
	for i := range resources {
		row := &RunHostResult{ID: resources[i].ID}
		if err := decodeResourceAttributes(resources[i].Attributes, row); err != nil {
			return nil, fmt.Errorf("decode host result %q: %w", resources[i].ID, err)
		}
		out = append(out, row)
	}
	return out, nil
}

// ListRunHostEvents returns a run's failure and unreachable events. An empty
// host returns every host's; otherwise only that host's. Requires read on the
// workspace.
func (c *Client) ListRunHostEvents(ctx context.Context, runID, host string) ([]*RunHostEvent, error) {
	base, err := runPath(runID)
	if err != nil {
		return nil, err
	}
	path := base + "/host-events"
	if host != "" {
		path += "?host=" + url.QueryEscape(host)
	}
	data, err := c.Get(ctx, path)
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, fmt.Errorf("parse host events response: %w", err)
	}
	out := make([]*RunHostEvent, 0, len(resources))
	for i := range resources {
		row := &RunHostEvent{ID: resources[i].ID}
		if err := decodeResourceAttributes(resources[i].Attributes, row); err != nil {
			return nil, fmt.Errorf("decode host event %q: %w", resources[i].ID, err)
		}
		out = append(out, row)
	}
	return out, nil
}

// GetRunHostEventStream returns the run's complete per-task event stream as
// JSONL -- one JSON object per line, in the order ansible reported them.
//
// # This is state-grade, and the server gates it accordingly
//
// The stream carries each module's whole return value for every event, `ok`
// ones included: a command's full stdout, a uri task's response body, whatever
// a lookup resolved. So the server requires `state:read` here, where
// ListRunHostResults and ListRunHostEvents need only `run:read` -- those serve
// the counters and ansible's own failure text truncated, which is the configure
// equivalent of a plan log. A caller holding run-tier access gets 403 from this
// one and should fall back to those two.
//
// # A 410 is not a 404, and the difference is the whole reason to check
//
// The stream lives in object storage under artifact retention, so it is deleted
// while the per-host summary rows remain in the database. The server answers
// 410 for that and 404 when the run reported no hosts at all. Use
// IsDetailReaped to tell them apart: presenting a reaped stream as "nothing
// happened" is the specific failure the status exists to prevent, because the
// run's own summary is still sitting beside it saying tasks ran.
//
// # host selects one host's own object rather than filtering
//
// Empty means the whole run. A host name fetches that host's partition, which
// the server writes at ingest alongside the whole stream -- so a drill-down is
// one object fetch rather than a download of a stream that is tens of MB at
// fleet scale. A partition is a byte-exact subsequence, so parse either the
// same way, and it holds everything that happened to the host rather than only
// its failures, which ListRunHostEvents already returns as rows.
//
// With a host named, the 410/404 split narrows to it: a reaped partition for a
// host the run reported on is 410, and a host it never reported on is 404,
// because nothing was lost there.
//
// The parameter rather than a second method, matching ListRunHostEvents, whose
// host argument means exactly this.
func (c *Client) GetRunHostEventStream(ctx context.Context, runID, host string) ([]byte, error) {
	base, err := runPath(runID)
	if err != nil {
		return nil, err
	}
	path := base + "/host-events/stream"
	if host != "" {
		path += "?host=" + url.QueryEscape(host)
	}
	return c.Get(ctx, path)
}

// GetRunHostEventPayloads returns the sidecar holding payloads too large to
// inline in the stream, as JSONL of {"ref": N, "payload": {...}}.
//
// An event in the main stream carries `payload_ref` instead of `payload` when
// its payload exceeded the inline bound; that integer is the `ref` here. One
// sidecar per run rather than an object per payload, so retention deletes a
// fixed key rather than enumerating a prefix.
//
// Same `state:read` gate as GetRunHostEventStream, more so: a payload reaches
// this file precisely by being the largest output the run produced. 404 means
// nothing spilled, which is the ordinary case and not a retention event --
// IsDetailReaped separates that from a sidecar that has been reaped.
func (c *Client) GetRunHostEventPayloads(ctx context.Context, runID string) ([]byte, error) {
	base, err := runPath(runID)
	if err != nil {
		return nil, err
	}
	return c.Get(ctx, base+"/host-events/payloads")
}

// IsDetailReaped returns true if err is the server saying an artifact existed
// and has been removed by retention (HTTP 410), rather than never having
// existed (404, IsNotFound).
//
// Kept beside the two readers above rather than in errors.go with IsNotFound
// and friends, because 410 is not a general shape in this API: these are the
// only endpoints that distinguish deleted from absent, and they do it because
// their siblings keep serving the summary after the detail is gone.
func IsDetailReaped(err error) bool {
	var ae *APIError
	return errors.As(err, &ae) && ae.StatusCode == http.StatusGone
}
