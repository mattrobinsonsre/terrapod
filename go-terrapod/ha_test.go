package terrapod

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func newHAFixture(t *testing.T, statusBody string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch r.URL.Path {
		case "/api/terrapod/v1/ha/status":
			_, _ = w.Write([]byte(statusBody))
		case "/api/terrapod/v1/ha/whoami":
			_, _ = w.Write([]byte(`{"data":{"id":"node-a","type":"ha-nodes",
			  "attributes":{"node-id":"node-a","role":"leader"}}}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	return c
}

const followerCaughtUp = `{"data":{"id":"node-b","type":"ha-status","attributes":{
  "node-id":"node-b","role":"follower","peer-configured":true,"replication-enabled":true,
  "last-sync-at":"2026-07-29T10:00:00Z","seconds-since-last-sync":42,
  "backfilling-classes":[],"in-sync":true,
  "events-retained":17,"oldest-event-age-seconds":3600,"retention-seconds":604800,
  "replicated-classes":["agent_pools","users"]}}}`

const followerBackfilling = `{"data":{"id":"node-b","type":"ha-status","attributes":{
  "node-id":"node-b","role":"follower","peer-configured":true,"replication-enabled":true,
  "last-sync-at":"2026-07-29T10:00:00Z","seconds-since-last-sync":5,
  "backfilling-classes":["api_tokens","users"],"in-sync":false,
  "events-retained":0,"retention-seconds":604800}}}`

func TestGetHAStatusCaughtUp(t *testing.T) {
	c := newHAFixture(t, followerCaughtUp)

	got, err := c.GetHAStatus(context.Background())
	if err != nil {
		t.Fatalf("GetHAStatus: %v", err)
	}
	if got.Role != "follower" || !got.InSync {
		t.Fatalf("role=%q in-sync=%v", got.Role, got.InSync)
	}
	if got.SecondsSinceLastSync != 42 {
		t.Fatalf("seconds-since-last-sync = %d", got.SecondsSinceLastSync)
	}
	if len(got.ReplicatedClasses) != 2 {
		t.Fatalf("replicated-classes = %v", got.ReplicatedClasses)
	}
}

// A node mid-backfill is NOT in sync however recent its last cycle — reading
// only the timestamp would say it is, which is the wrong answer to give
// somebody about to move DNS.
func TestGetHAStatusBackfillingIsNotInSync(t *testing.T) {
	c := newHAFixture(t, followerBackfilling)

	got, err := c.GetHAStatus(context.Background())
	if err != nil {
		t.Fatalf("GetHAStatus: %v", err)
	}
	if got.InSync {
		t.Fatal("a node with classes mid-backfill must not report in-sync")
	}
	if got.SecondsSinceLastSync != 5 {
		t.Fatalf("last sync was recent but the node is still backfilling: %d", got.SecondsSinceLastSync)
	}
	if len(got.BackfillingClasses) != 2 {
		t.Fatalf("backfilling-classes = %v", got.BackfillingClasses)
	}
}

// The leader's early warning: as the oldest retained event approaches the
// retention window, the follower is close to having to backfill from scratch.
func TestGetHAStatusRetentionMargin(t *testing.T) {
	c := newHAFixture(t, followerCaughtUp)

	got, err := c.GetHAStatus(context.Background())
	if err != nil {
		t.Fatalf("GetHAStatus: %v", err)
	}
	if got.OldestEventAgeSeconds >= got.RetentionSeconds {
		t.Fatalf("oldest=%d retention=%d", got.OldestEventAgeSeconds, got.RetentionSeconds)
	}
}

func TestWhoAmI(t *testing.T) {
	c := newHAFixture(t, followerCaughtUp)

	got, err := c.WhoAmI(context.Background())
	if err != nil {
		t.Fatalf("WhoAmI: %v", err)
	}
	if got.NodeID != "node-a" || got.Role != "leader" {
		t.Fatalf("got %+v", got)
	}
}

func TestGetHAStatusPropagatesErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"errors":[{"detail":"admin or audit required"}]}`))
	}))
	t.Cleanup(srv.Close)
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	if _, err := c.GetHAStatus(context.Background()); err == nil {
		t.Fatal("expected an error for a non-admin caller")
	}
}
