package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newNotifFixture(t *testing.T) (*Client, *[]byte) {
	t.Helper()
	var lastBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/notification-configurations"):
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"nc-aaa","type":"notification-configurations","attributes":{
			  "name":"slack-prod","destination-type":"slack","url":"https://hooks.slack.com/...",
			  "enabled":true,"has-token":true,"triggers":["run:completed","run:errored"]
			}}}`))
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/notification-configurations"):
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"nc-aaa","type":"notification-configurations","attributes":{"name":"slack-prod","destination-type":"slack","enabled":true}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/notification-configurations/"):
			_, _ = w.Write([]byte(`{"data":{"id":"nc-aaa","type":"notification-configurations","attributes":{"name":"slack-prod","destination-type":"slack","enabled":true,"has-token":true}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"nc-aaa","type":"notification-configurations","attributes":{"name":"slack-prod","destination-type":"slack","enabled":false}}}`))
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
	return c, &lastBody
}

func TestCreateNotificationConfiguration_Slack(t *testing.T) {
	c, lastBody := newNotifFixture(t)
	nc, err := c.CreateNotificationConfiguration(t.Context(), "ws-app", CreateNotificationConfigurationRequest{
		Name:            "slack-prod",
		DestinationType: "slack",
		URL:             "https://hooks.slack.com/...",
		Token:           "shh",
		Enabled:         true,
		Triggers:        []string{"run:completed", "run:errored"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if nc.DestinationType != "slack" || len(nc.Triggers) != 2 {
		t.Errorf("nc: %+v", nc)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["token"] != "shh" {
		t.Errorf("token should be in request: %+v", req.Data.Attributes)
	}
}

func TestUpdateNotificationConfiguration_LeaveTokenAlone(t *testing.T) {
	// Vanilla PATCH that toggles enabled should NOT include token.
	c, lastBody := newNotifFixture(t)
	off := false
	_, err := c.UpdateNotificationConfiguration(t.Context(), "nc-aaa", UpdateNotificationConfigurationRequest{
		Enabled: &off,
	})
	if err != nil {
		t.Fatal(err)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if _, has := req.Data.Attributes["token"]; has {
		t.Errorf("token leaked into PATCH: %+v", req.Data.Attributes)
	}
}

func TestGetNotificationConfiguration(t *testing.T) {
	c, _ := newNotifFixture(t)
	nc, err := c.GetNotificationConfiguration(t.Context(), "nc-aaa")
	if err != nil {
		t.Fatal(err)
	}
	if !nc.HasToken {
		t.Errorf("nc: %+v", nc)
	}
}

func TestListNotificationConfigurations(t *testing.T) {
	c, _ := newNotifFixture(t)
	list, err := c.ListNotificationConfigurations(t.Context(), "ws-app")
	if err != nil || len(list) != 1 {
		t.Errorf("list: %v / %v", list, err)
	}
}

func TestDeleteNotificationConfiguration(t *testing.T) {
	c, _ := newNotifFixture(t)
	if err := c.DeleteNotificationConfiguration(t.Context(), "nc-aaa"); err != nil {
		t.Error(err)
	}
}
