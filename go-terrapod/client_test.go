package terrapod

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestNewClient_RequiresBaseURLAndToken(t *testing.T) {
	if _, err := NewClient(Options{}); err == nil {
		t.Error("expected error for empty options")
	}
	if _, err := NewClient(Options{BaseURL: "https://terrapod.example"}); err == nil {
		t.Error("expected error for missing Token")
	}
	if _, err := NewClient(Options{Token: "t"}); err == nil {
		t.Error("expected error for missing BaseURL")
	}
}

func TestNewClient_NormalisesBaseURL(t *testing.T) {
	cases := []struct{ in, want string }{
		{"terrapod.example.com", "https://terrapod.example.com"},
		{"https://terrapod.example.com", "https://terrapod.example.com"},
		{"https://terrapod.example.com/", "https://terrapod.example.com"},
		{"http://terrapod-dev.example", "http://terrapod-dev.example"},
		{"  https://terrapod.example.com  ", "https://terrapod.example.com"},
	}
	for _, c := range cases {
		client, err := NewClient(Options{BaseURL: c.in, Token: "t"})
		if err != nil {
			t.Fatalf("NewClient(%q): %v", c.in, err)
		}
		if client.BaseURL != c.want {
			t.Errorf("NewClient(%q).BaseURL = %q, want %q", c.in, client.BaseURL, c.want)
		}
	}
}

func TestNewClient_DefaultsUserAgentAndMaxRetries(t *testing.T) {
	c, err := NewClient(Options{BaseURL: "https://x", Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(c.UserAgent, "go-terrapod/") {
		t.Errorf("default UserAgent should start with go-terrapod/, got %q", c.UserAgent)
	}
	if c.MaxRetries != 3 {
		t.Errorf("default MaxRetries = %d, want 3", c.MaxRetries)
	}
}

func TestClient_GET_HappyPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v2/x" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer t" {
			t.Errorf("missing bearer token")
		}
		if r.Header.Get("Content-Type") != "application/vnd.api+json" {
			t.Errorf("content-type missing")
		}
		_, _ = w.Write([]byte(`{"data":{"id":"x","type":"things","attributes":{}}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	body, err := c.Get(t.Context(), "/api/v2/x")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if !strings.Contains(string(body), `"id":"x"`) {
		t.Errorf("unexpected body: %s", body)
	}
}

func TestClient_NotFoundReturnsTypedError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"errors":[{"status":"404","title":"Not Found","detail":"workspace ws-x"}]}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	_, err := c.Get(t.Context(), "/api/v2/workspaces/ws-x")
	if !IsNotFound(err) {
		t.Errorf("expected NotFoundError, got: %v", err)
	}
}

func TestClient_409Conflict_422Validation_401Auth_403AuthZ(t *testing.T) {
	cases := []struct {
		status   int
		typeName string
		check    func(error) bool
	}{
		{http.StatusConflict, "ConflictError", IsConflict},
		{http.StatusUnprocessableEntity, "ValidationError", IsValidation},
		{http.StatusUnauthorized, "AuthenticationError", IsAuth},
		{http.StatusForbidden, "AuthorizationError", IsAuth},
	}
	for _, c := range cases {
		t.Run(c.typeName, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(c.status)
				_, _ = w.Write([]byte(fmt.Sprintf(`{"errors":[{"status":"%d","detail":"d"}]}`, c.status)))
			}))
			defer srv.Close()
			client, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
			_, err := client.Get(t.Context(), "/x")
			if !c.check(err) {
				t.Errorf("expected %s, got: %v", c.typeName, err)
			}
		})
	}
}

func TestClient_GenericAPIErrorOnUnknownStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTeapot)
		_, _ = w.Write([]byte(`I'm a teapot`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	_, err := c.Get(t.Context(), "/x")
	var api *APIError
	if !errors.As(err, &api) || api.StatusCode != http.StatusTeapot {
		t.Errorf("expected *APIError(418), got: %v", err)
	}
}

func TestClient_5xxRetriesUntilSuccess(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n < 3 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		_, _ = w.Write([]byte(`{"data":{}}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{
		BaseURL:    srv.URL,
		Token:      "t",
		MaxRetries: 3,
		// Speed the test by shrinking the http client's timeout —
		// don't need real seconds-long backoffs.
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
	})
	body, err := c.Get(t.Context(), "/x")
	if err != nil {
		t.Fatalf("expected eventual success, got: %v", err)
	}
	if !strings.Contains(string(body), `"data":{}`) {
		t.Errorf("body: %s", body)
	}
	if calls.Load() != 3 {
		t.Errorf("expected 3 calls, got %d", calls.Load())
	}
}

func TestClient_5xxRetriesExhaust(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer srv.Close()

	c, _ := NewClient(Options{
		BaseURL:    srv.URL,
		Token:      "t",
		MaxRetries: 2,
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
	})
	_, err := c.Get(t.Context(), "/x")
	if err == nil {
		t.Fatal("expected exhausted-retries error")
	}
	if calls.Load() != 3 { // initial + 2 retries
		t.Errorf("expected 3 attempts, got %d", calls.Load())
	}
}

func TestClient_429Retries(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n < 2 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	c, _ := NewClient(Options{
		BaseURL:    srv.URL,
		Token:      "t",
		MaxRetries: 3,
	})
	if _, err := c.Get(t.Context(), "/x"); err != nil {
		t.Errorf("429 should retry: %v", err)
	}
}

func TestClient_PostPatchPutDelete(t *testing.T) {
	var lastMethod, lastPath, lastBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lastMethod = r.Method
		lastPath = r.URL.Path
		b, _ := io.ReadAll(r.Body)
		lastBody = string(b)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"data":{}}`))
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	for _, op := range []struct {
		name   string
		do     func() error
		method string
		path   string
		body   string
	}{
		{"Post", func() error { _, err := c.Post(t.Context(), "/p", []byte(`{"x":1}`)); return err }, "POST", "/p", `{"x":1}`},
		{"Patch", func() error { _, err := c.Patch(t.Context(), "/p2", []byte(`{"y":2}`)); return err }, "PATCH", "/p2", `{"y":2}`},
		{"Put", func() error { _, err := c.Put(t.Context(), "/p3", []byte(`{"z":3}`)); return err }, "PUT", "/p3", `{"z":3}`},
		{"Delete", func() error { return c.Delete(t.Context(), "/p4") }, "DELETE", "/p4", ""},
		{"DeleteWithBody", func() error { return c.DeleteWithBody(t.Context(), "/p5", []byte(`{"k":1}`)) }, "DELETE", "/p5", `{"k":1}`},
	} {
		t.Run(op.name, func(t *testing.T) {
			if err := op.do(); err != nil {
				t.Fatal(err)
			}
			if lastMethod != op.method || lastPath != op.path || lastBody != op.body {
				t.Errorf("%s: method=%q path=%q body=%q", op.name, lastMethod, lastPath, lastBody)
			}
		})
	}
}

func TestClient_ContextCancellation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Hold the request open until the context cancels.
		time.Sleep(2 * time.Second)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	c, _ := NewClient(Options{BaseURL: srv.URL, Token: "t"})

	ctx, cancel := context.WithTimeout(t.Context(), 100*time.Millisecond)
	defer cancel()
	_, err := c.Get(ctx, "/x")
	if err == nil {
		t.Fatal("expected ctx-cancellation error")
	}
}
