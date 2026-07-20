package mcpserver

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// A file-sourced token that has rotated: the first (stale) call 401s, the MCP
// re-reads the credentials file, and the retry with the fresh token succeeds —
// no reconnect needed (issue #880).
func TestRefreshTransportRetriesOn401WithFreshToken(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer new" { // server only accepts the fresh token
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	rt := &refreshTransport{base: http.DefaultTransport, token: "old", refresh: func() string { return "new" }}
	client := &http.Client{Transport: rt}

	req, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
	req.Header.Set("Authorization", "Bearer old") // what the go-terrapod client sets from its startup token
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d, want 200 after refresh+retry", resp.StatusCode)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("calls=%d, want 2 (stale 401 + fresh retry)", got)
	}
	if rt.current() != "new" {
		t.Fatalf("cached token=%q, want new", rt.current())
	}

	// Subsequent requests use the cached fresh token on the FIRST attempt, even
	// though the client keeps setting the stale startup token.
	calls.Store(0)
	req2, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
	req2.Header.Set("Authorization", "Bearer old")
	resp2, err := client.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("status=%d, want 200", resp2.StatusCode)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("calls=%d, want 1 (proactive fresh token, no re-401)", got)
	}
}

// A POST body is replayed on the retry (net/http gives GetBody for the
// bytes/strings readers go-terrapod sends).
func TestRefreshTransportReplaysBodyOnRetry(t *testing.T) {
	var gotBody atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer new" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		b, _ := io.ReadAll(r.Body)
		gotBody.Store(string(b))
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	rt := &refreshTransport{base: http.DefaultTransport, token: "old", refresh: func() string { return "new" }}
	client := &http.Client{Transport: rt}

	req, _ := http.NewRequest(http.MethodPost, srv.URL, strings.NewReader(`{"payload":1}`))
	req.Header.Set("Authorization", "Bearer old")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d, want 200", resp.StatusCode)
	}
	if got, _ := gotBody.Load().(string); got != `{"payload":1}` {
		t.Fatalf("retry body=%q, want the original payload replayed", got)
	}
}

// An explicit token (--token / $TERRAPOD_TOKEN → refresh == nil) is never
// re-read: a genuinely-bad explicit token surfaces its 401 immediately.
func TestRefreshTransportFixedTokenDoesNotRetry(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	rt := &refreshTransport{base: http.DefaultTransport, token: "bad", refresh: nil}
	client := &http.Client{Transport: rt}
	req, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d, want 401 passthrough", resp.StatusCode)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("calls=%d, want 1 (no retry for a fixed token)", got)
	}
}

// When the re-resolved token is unchanged (the file hasn't actually rotated),
// don't loop — surface the 401 once.
func TestRefreshTransportNoRetryWhenTokenUnchanged(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	rt := &refreshTransport{base: http.DefaultTransport, token: "same", refresh: func() string { return "same" }}
	client := &http.Client{Transport: rt}
	req, _ := http.NewRequest(http.MethodGet, srv.URL, nil)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if got := calls.Load(); got != 1 {
		t.Fatalf("calls=%d, want 1 (no retry when re-resolved token is unchanged)", got)
	}
}
