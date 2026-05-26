package terrapod

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newRSCFixture(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			_, _ = io.ReadAll(r.Body)
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/remote-state-consumers"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"rsc-aaa","type":"remote-state-consumers","attributes":{
			  "producer-workspace-name":"prod","consumer-workspace-name":"app","created-by":"alice@example.com"
			},"relationships":{
			  "producer":{"data":{"id":"ws-prod","type":"workspaces"}},
			  "consumer":{"data":{"id":"ws-app","type":"workspaces"}}
			}}}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/remote-state-consumers/"):
			_, _ = w.Write([]byte(`{"data":{"id":"rsc-aaa","type":"remote-state-consumers","relationships":{
			  "producer":{"data":{"id":"ws-prod","type":"workspaces"}},
			  "consumer":{"data":{"id":"ws-app","type":"workspaces"}}
			}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/remote-state-consumers"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"rsc-aaa","type":"remote-state-consumers","relationships":{
			    "producer":{"data":{"id":"ws-prod","type":"workspaces"}},
			    "consumer":{"data":{"id":"ws-app","type":"workspaces"}}}}
			]}`))
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "unhandled", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestCreateRemoteStateConsumer(t *testing.T) {
	c := newRSCFixture(t)
	rsc, err := c.CreateRemoteStateConsumer(t.Context(), CreateRemoteStateConsumerRequest{
		ProducerWorkspaceID: "ws-prod",
		ConsumerWorkspaceID: "ws-app",
	})
	if err != nil {
		t.Fatal(err)
	}
	if rsc.ProducerWorkspaceID != "ws-prod" || rsc.ConsumerWorkspaceID != "ws-app" {
		t.Errorf("rsc: %+v", rsc)
	}
}

func TestListRemoteStateConsumers(t *testing.T) {
	c := newRSCFixture(t)
	list, err := c.ListRemoteStateConsumers(t.Context(), "ws-prod")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 {
		t.Errorf("list: %+v", list)
	}
}

func TestDeleteRemoteStateConsumer(t *testing.T) {
	c := newRSCFixture(t)
	if err := c.DeleteRemoteStateConsumer(t.Context(), "rsc-aaa"); err != nil {
		t.Error(err)
	}
}
