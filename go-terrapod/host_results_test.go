package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newHostResultsFixture(t *testing.T, status int, body string) (*Client, *[]string) {
	t.Helper()
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.RequestURI())
		w.Header().Set("Content-Type", "application/vnd.api+json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"errors":[{"detail":"nope"}]}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c, &seen
}

// Kebab-case throughout, because that is the house style the API emits — a
// snake_case fixture would pass against snake_case tags and fail in production.
const hostResultsBody = `{"data":[
  {"id":"rhr-1","type":"run-host-results","attributes":{
     "host":"web1","status":"ok","ok":5,"changed":0,"failures":0,"unreachable":0,
     "skipped":1,"rescued":0,"ignored":0,"saw-unreachable":false,"reached":true,
     "created-at":"2026-10-23T00:00:00Z","updated-at":"2026-10-23T00:00:01Z"}},
  {"id":"rhr-2","type":"run-host-results","attributes":{
     "host":"hdead","status":"unreachable","ok":2,"changed":0,"failures":0,
     "unreachable":0,"skipped":0,"rescued":0,"ignored":1,
     "saw-unreachable":true,"reached":true}},
  {"id":"rhr-3","type":"run-host-results","attributes":{
     "host":"web9","status":"not-reached","ok":null,"changed":null,"failures":null,
     "unreachable":null,"skipped":null,"rescued":null,"ignored":null,
     "saw-unreachable":false,"reached":false}}
], "meta":{"pagination":{"current-page":1,"page-size":3,"total-count":3,"total-pages":1}}}`

func TestListRunHostResults(t *testing.T) {
	c, seen := newHostResultsFixture(t, http.StatusOK, hostResultsBody)
	rows, err := c.ListRunHostResults(context.Background(), "abc")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 {
		t.Fatalf("want 3 rows, got %d", len(rows))
	}
	if got := (*seen)[0]; got != "/api/v1/runs/run-abc/host-results" {
		t.Errorf("path %q — the id must be prefixed and the canonical prefix used", got)
	}

	ok := rows[0]
	if ok.Host != "web1" || ok.Status != "ok" || ok.Reached != true {
		t.Errorf("reported host decoded wrong: %+v", ok)
	}
	if ok.OK == nil || *ok.OK != 5 || ok.Skipped == nil || *ok.Skipped != 1 {
		t.Errorf("counters decoded wrong: %+v", ok)
	}
}

// The measured disagreement: the counter says zero while the event fired, so a
// consumer reading Unreachable instead of SawUnreachable calls a dead host
// healthy.
func TestListRunHostResultsSawUnreachableIsNotTheCounter(t *testing.T) {
	c, _ := newHostResultsFixture(t, http.StatusOK, hostResultsBody)
	rows, err := c.ListRunHostResults(context.Background(), "run-abc")
	if err != nil {
		t.Fatal(err)
	}
	dead := rows[1]
	if dead.Unreachable == nil || *dead.Unreachable != 0 {
		t.Fatalf("fixture premise changed: %+v", dead)
	}
	if !dead.SawUnreachable {
		t.Error("SawUnreachable did not decode — it is the only honest signal here")
	}
	if dead.Status != "unreachable" {
		t.Errorf("status %q, want unreachable", dead.Status)
	}
}

// nil rather than zero on a host nothing reported: zero is a measurement and
// this is the absence of one, so a consumer summing counters must be able to
// tell them apart.
func TestListRunHostResultsNotReachedCarriesNilCounters(t *testing.T) {
	c, _ := newHostResultsFixture(t, http.StatusOK, hostResultsBody)
	rows, err := c.ListRunHostResults(context.Background(), "run-abc")
	if err != nil {
		t.Fatal(err)
	}
	absent := rows[2]
	if absent.Reached {
		t.Error("Reached should be false for a host ansible never mentioned")
	}
	if absent.OK != nil || absent.Failures != nil {
		t.Errorf("want nil counters, got %+v", absent)
	}
}

const hostEventsBody = `{"data":[
  {"id":"rhe-1","type":"run-host-events","attributes":{
     "host":"web2","kind":"failed","task-name":"restart nginx",
     "task-action":"ansible.builtin.service","task-uuid":"16b8-0007",
     "play-name":"configure","ignored":true,"message":"non-zero return code",
     "payload-ref":null,"sequence":42,"created-at":"2026-10-23T00:00:00Z"}},
  {"id":"rhe-2","type":"run-host-events","attributes":{
     "host":"hdead","kind":"unreachable","task-name":"ping","task-action":"ansible.builtin.ping",
     "task-uuid":"16b8-000f","play-name":"configure","ignored":false,
     "message":"No route to host","payload-ref":7,"sequence":43}}
], "meta":{"pagination":{"current-page":1,"page-size":2,"total-count":2,"total-pages":1}}}`

func TestListRunHostEvents(t *testing.T) {
	c, seen := newHostResultsFixture(t, http.StatusOK, hostEventsBody)
	rows, err := c.ListRunHostEvents(context.Background(), "abc", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("want 2, got %d", len(rows))
	}
	if got := (*seen)[0]; got != "/api/v1/runs/run-abc/host-events" {
		t.Errorf("an empty host must add no query: %q", got)
	}
	first := rows[0]
	if first.TaskName != "restart nginx" || first.TaskAction != "ansible.builtin.service" {
		t.Errorf("kebab-case attributes did not decode: %+v", first)
	}
	if !first.Ignored || first.Sequence != 42 {
		t.Errorf("ignored/sequence decoded wrong: %+v", first)
	}
	if first.PayloadRef != nil {
		t.Errorf("an inline payload must leave PayloadRef nil, got %v", *first.PayloadRef)
	}
	if rows[1].PayloadRef == nil || *rows[1].PayloadRef != 7 {
		t.Errorf("a spilled payload must carry its reference: %+v", rows[1])
	}
}

func TestListRunHostEventsFiltersByHost(t *testing.T) {
	c, seen := newHostResultsFixture(t, http.StatusOK, hostEventsBody)
	if _, err := c.ListRunHostEvents(context.Background(), "run-abc", "web 2"); err != nil {
		t.Fatal(err)
	}
	got := (*seen)[0]
	if !strings.Contains(got, "host=web+2") && !strings.Contains(got, "host=web%202") {
		t.Errorf("the host must be query-escaped: %q", got)
	}
}

func TestHostResultsRequireARunID(t *testing.T) {
	c, _ := newHostResultsFixture(t, http.StatusOK, hostResultsBody)
	if _, err := c.ListRunHostResults(context.Background(), ""); err == nil {
		t.Error("an empty run id must be refused rather than requesting /runs//host-results")
	}
	if _, err := c.ListRunHostEvents(context.Background(), "", ""); err == nil {
		t.Error("an empty run id must be refused")
	}
}

func TestHostResultsSurfaceServerErrors(t *testing.T) {
	c, _ := newHostResultsFixture(t, http.StatusForbidden, "")
	if _, err := c.ListRunHostResults(context.Background(), "run-abc"); err == nil {
		t.Error("a 403 must be returned as an error, not an empty list")
	}
	if _, err := c.ListRunHostEvents(context.Background(), "run-abc", ""); err == nil {
		t.Error("a 403 must be returned as an error, not an empty list")
	}
}
