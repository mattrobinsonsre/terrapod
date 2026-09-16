package terrapod

import (
	"net/http/httptest"
	"reflect"
	"testing"
)

// A templated Vault file reference (#1648) carries a multi-line template with
// `{{ … }}`, quotes and newlines, and has no `field`. The SDK must still treat
// the reference as an opaque string: JSON string escaping must not change a
// byte, and Go's HTML-safe escaping (`<`, `>`, `&` → < …) must not leak
// into the wire value either, since a template may contain any of them.
const templateRef = `{"source":"vault","engine":"dynamic","mount":"aws","path":"creds/deploy","file":{"name":"~/.aws/credentials","template":"[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key | trim }}\n# <expires> {{ _lease.expires_at }} & \"quoted\"\n"}}`
const formatRef = `{"source":"vault","mount":"secret","path":"apps/db","file":{"name":"db.env","format":"env","fields":["DB_USER","DB_PASS"]}}`

func TestWorkspaceVariableCarriesATemplateReferenceVerbatim(t *testing.T) {
	for name, ref := range map[string]string{"template": templateRef, "format": formatRef} {
		t.Run(name, func(t *testing.T) {
			f := &fakeVaultVarServer{}
			srv := httptest.NewServer(f.handler(t, "/api/v2/workspaces/ws-1/vars"))
			defer srv.Close()
			c := mustVarClient(t, srv)

			created, err := c.CreateVariable(t.Context(), "ws-1", CreateVariableRequest{
				Key: "AWS_SHARED_CREDENTIALS_FILE", Category: "env", Value: ref, ValueSource: "vault",
			})
			if err != nil {
				t.Fatalf("CreateVariable: %v", err)
			}
			want := map[string]any{
				"key": "AWS_SHARED_CREDENTIALS_FILE", "category": "env",
				"value": ref, "value-source": "vault",
			}
			if got := attrsOf(t, f.writes[0]); !reflect.DeepEqual(got, want) {
				t.Errorf("create sent\n  %v\nwant\n  %v", got, want)
			}
			if created.Value != ref {
				t.Errorf("create returned a different reference:\n  %s\nwant\n  %s", created.Value, ref)
			}

			read, err := c.GetVariable(t.Context(), "ws-1", "var-1")
			if err != nil {
				t.Fatalf("GetVariable: %v", err)
			}
			if read.Value != ref {
				t.Errorf("read changed the reference:\n  %s\nwant\n  %s", read.Value, ref)
			}
		})
	}
}

func TestVarsetVariableCarriesATemplateReferenceVerbatim(t *testing.T) {
	f := &fakeVaultVarServer{}
	srv := httptest.NewServer(f.handler(t, "/api/v2/varsets/varset-1/relationships/vars"))
	defer srv.Close()
	c := mustVarClient(t, srv)

	newValue := templateRef
	if _, err := c.CreateVarsetVariable(t.Context(), "varset-1", CreateVarsetVariableRequest{
		Key: "AWS_SHARED_CREDENTIALS_FILE", Category: "env", Value: formatRef, ValueSource: "vault",
	}); err != nil {
		t.Fatalf("CreateVarsetVariable: %v", err)
	}
	updated, err := c.UpdateVarsetVariable(t.Context(), "varset-1", "var-1", UpdateVarsetVariableRequest{Value: &newValue})
	if err != nil {
		t.Fatalf("UpdateVarsetVariable: %v", err)
	}
	if got := attrsOf(t, f.writes[1]); !reflect.DeepEqual(got, map[string]any{"value": templateRef}) {
		t.Errorf("update sent %v, want only the new value", got)
	}
	if updated.Value != templateRef {
		t.Errorf("update returned %q, want %q", updated.Value, templateRef)
	}
}
