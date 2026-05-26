package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newUserFixture(t *testing.T) (*Client, *[]byte) {
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
		case r.Method == http.MethodPost && r.URL.Path == "/api/terrapod/v1/users":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"data":{"id":"alice@example.com","type":"users","attributes":{
			  "email":"alice@example.com","display-name":"Alice","is-active":true,"has-password":true
			}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/users":
			_, _ = w.Write([]byte(`{"data":[
			  {"id":"alice@example.com","type":"users","attributes":{"email":"alice@example.com"}},
			  {"id":"bob@example.com","type":"users","attributes":{"email":"bob@example.com","is-active":false}}
			]}`))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/users/"):
			_, _ = w.Write([]byte(`{"data":{"id":"alice@example.com","type":"users","attributes":{"email":"alice@example.com","display-name":"Alice"}}}`))
		case r.Method == http.MethodPatch:
			_, _ = w.Write([]byte(`{"data":{"id":"alice@example.com","type":"users","attributes":{"email":"alice@example.com","display-name":"Alice Cooper","is-active":false}}}`))
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

func TestCreateUser_WithPassword(t *testing.T) {
	c, lastBody := newUserFixture(t)
	u, err := c.CreateUser(t.Context(), CreateUserRequest{
		Email:       "alice@example.com",
		DisplayName: "Alice",
		Password:    "hunter2",
	})
	if err != nil {
		t.Fatal(err)
	}
	if u.Email != "alice@example.com" || !u.HasPassword {
		t.Errorf("user: %+v", u)
	}
	var req struct {
		Data struct {
			Attributes map[string]any `json:"attributes"`
		} `json:"data"`
	}
	_ = json.Unmarshal(*lastBody, &req)
	if req.Data.Attributes["password"] != "hunter2" {
		t.Errorf("password not in request: %+v", req.Data.Attributes)
	}
}

func TestCreateUser_SSOOnly_NoPassword(t *testing.T) {
	c, lastBody := newUserFixture(t)
	_, err := c.CreateUser(t.Context(), CreateUserRequest{
		Email: "alice@example.com",
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
	if _, has := req.Data.Attributes["password"]; has {
		t.Errorf("password should be omitted for SSO-only user: %+v", req.Data.Attributes)
	}
}

func TestGetUser(t *testing.T) {
	c, _ := newUserFixture(t)
	u, err := c.GetUser(t.Context(), "alice@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if u.DisplayName != "Alice" {
		t.Errorf("user: %+v", u)
	}
}

func TestListUsers(t *testing.T) {
	c, _ := newUserFixture(t)
	users, err := c.ListUsers(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(users) != 2 {
		t.Fatalf("got %d users", len(users))
	}
	if users[1].IsActive {
		t.Errorf("bob should be inactive: %+v", users[1])
	}
}

func TestUpdateUser_DeactivateExplicitly(t *testing.T) {
	c, lastBody := newUserFixture(t)
	off := false
	_, err := c.UpdateUser(t.Context(), "alice@example.com", UpdateUserRequest{
		IsActive: &off,
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
	v, has := req.Data.Attributes["is-active"]
	if !has {
		t.Fatal("is-active missing from body")
	}
	if v.(bool) {
		t.Errorf("is-active should be false, got: %v", v)
	}
}

func TestDeleteUser(t *testing.T) {
	c, _ := newUserFixture(t)
	if err := c.DeleteUser(t.Context(), "alice@example.com"); err != nil {
		t.Error(err)
	}
}
