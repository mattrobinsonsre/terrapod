package terrapod

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newAssignmentFixture(t *testing.T, initial []RoleAssignment) (*Client, *[]byte, *[]RoleAssignment) {
	t.Helper()
	store := make([]RoleAssignment, len(initial))
	copy(store, initial)
	var lastBody []byte

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			b, _ := io.ReadAll(r.Body)
			lastBody = b
			_ = r.Body.Close()
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")

		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/terrapod/v1/role-assignments":
			items := make([]map[string]any, 0, len(store))
			for _, a := range store {
				items = append(items, map[string]any{
					"type": "role-assignments",
					"attributes": map[string]any{
						"provider-name": a.ProviderName,
						"email":         a.Email,
						"role-name":     a.RoleName,
						"created-at":    "2025-01-01T00:00:00Z",
					},
				})
			}
			b, _ := json.Marshal(map[string]any{"data": items})
			_, _ = w.Write(b)
		case r.Method == http.MethodPut && r.URL.Path == "/api/terrapod/v1/role-assignments":
			var doc struct {
				Data struct {
					Attributes struct {
						ProviderName string   `json:"provider-name"`
						Email        string   `json:"email"`
						Roles        []string `json:"roles"`
					} `json:"attributes"`
				} `json:"data"`
			}
			_ = json.Unmarshal(lastBody, &doc)
			// Drop existing assignments for this identity, then re-add.
			next := store[:0]
			for _, a := range store {
				if !(a.ProviderName == doc.Data.Attributes.ProviderName && a.Email == doc.Data.Attributes.Email) {
					next = append(next, a)
				}
			}
			for _, role := range doc.Data.Attributes.Roles {
				next = append(next, RoleAssignment{
					ProviderName: doc.Data.Attributes.ProviderName,
					Email:        doc.Data.Attributes.Email,
					RoleName:     role,
				})
			}
			store = next
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/terrapod/v1/role-assignments/"):
			parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/terrapod/v1/role-assignments/"), "/")
			if len(parts) != 3 {
				http.Error(w, "bad path", http.StatusBadRequest)
				return
			}
			next := store[:0]
			for _, a := range store {
				if !(a.ProviderName == parts[0] && a.Email == parts[1] && a.RoleName == parts[2]) {
					next = append(next, a)
				}
			}
			store = next
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
	return c, &lastBody, &store
}

func TestListRoleAssignments(t *testing.T) {
	c, _, _ := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
		{ProviderName: "local", Email: "bob@example.com", RoleName: "sre"},
	})
	list, err := c.ListRoleAssignments(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Errorf("got %d assignments", len(list))
	}
}

func TestListRoleAssignmentsForIdentity(t *testing.T) {
	c, _, _ := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
		{ProviderName: "local", Email: "alice@example.com", RoleName: "sre"},
		{ProviderName: "local", Email: "bob@example.com", RoleName: "sre"},
	})
	list, err := c.ListRoleAssignmentsForIdentity(t.Context(), "local", "alice@example.com")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Errorf("expected 2 alice assignments, got %d", len(list))
	}
}

func TestAddRoleToIdentity_Idempotent(t *testing.T) {
	c, _, store := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
	})
	if err := c.AddRoleToIdentity(t.Context(), "local", "alice@example.com", "sre"); err != nil {
		t.Fatal(err)
	}
	if err := c.AddRoleToIdentity(t.Context(), "local", "alice@example.com", "sre"); err != nil {
		// Second add should be a no-op, not an error.
		t.Errorf("second add should not error: %v", err)
	}
	if len(*store) != 2 {
		t.Errorf("expected 2 assignments after idempotent add, got %d: %+v", len(*store), *store)
	}
}

func TestRemoveRoleFromIdentity(t *testing.T) {
	c, _, store := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
		{ProviderName: "local", Email: "alice@example.com", RoleName: "sre"},
	})
	if err := c.RemoveRoleFromIdentity(t.Context(), "local", "alice@example.com", "sre"); err != nil {
		t.Fatal(err)
	}
	if len(*store) != 1 || (*store)[0].RoleName != "admin" {
		t.Errorf("expected only admin remaining: %+v", *store)
	}
}

func TestSetRolesForIdentity_ReplaceAll(t *testing.T) {
	c, _, store := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
		{ProviderName: "local", Email: "alice@example.com", RoleName: "sre"},
	})
	if err := c.SetRolesForIdentity(t.Context(), "local", "alice@example.com", []string{"audit"}); err != nil {
		t.Fatal(err)
	}
	if len(*store) != 1 || (*store)[0].RoleName != "audit" {
		t.Errorf("expected only audit after set: %+v", *store)
	}
}

func TestSetRolesForIdentity_EmptyClearsAll(t *testing.T) {
	c, _, store := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
	})
	if err := c.SetRolesForIdentity(t.Context(), "local", "alice@example.com", nil); err != nil {
		t.Fatal(err)
	}
	if len(*store) != 0 {
		t.Errorf("expected empty store: %+v", *store)
	}
}

func TestGetRoleAssignment(t *testing.T) {
	c, _, _ := newAssignmentFixture(t, []RoleAssignment{
		{ProviderName: "local", Email: "alice@example.com", RoleName: "admin"},
	})
	a, err := c.GetRoleAssignment(t.Context(), "local", "alice@example.com", "admin")
	if err != nil || a == nil {
		t.Fatalf("expected assignment, got %v / err %v", a, err)
	}
	missing, err := c.GetRoleAssignment(t.Context(), "local", "alice@example.com", "nope")
	if err == nil || !IsNotFound(err) {
		t.Fatalf("expected NotFoundError, got %v (missing=%+v)", err, missing)
	}
	if missing != nil {
		t.Errorf("expected nil for missing assignment, got %+v", missing)
	}
}
