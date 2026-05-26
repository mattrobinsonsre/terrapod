package terrapod

import (
	"encoding/json"
	"testing"
)

func TestMarshalResource_WithAndWithoutRelationships(t *testing.T) {
	// Without rels — basic create body shape.
	body, err := MarshalResource("workspaces", map[string]any{"name": "api-prod"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatal(err)
	}
	data := got["data"].(map[string]any)
	if data["type"] != "workspaces" || data["attributes"].(map[string]any)["name"] != "api-prod" {
		t.Errorf("unexpected body: %s", body)
	}
	if _, has := data["relationships"]; has {
		t.Error("nil relationships should be omitted")
	}

	// With rels.
	body, err = MarshalResource("variables",
		map[string]any{"key": "region"},
		map[string]any{"workspace": map[string]any{"data": map[string]any{"id": "ws-x", "type": "workspaces"}}},
	)
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(body, &got)
	data = got["data"].(map[string]any)
	if _, has := data["relationships"]; !has {
		t.Error("relationships should be present")
	}
}

func TestMarshalResourceWithID(t *testing.T) {
	body, _ := MarshalResourceWithID("ws-abc", "workspaces", map[string]any{"name": "x"})
	var got map[string]any
	_ = json.Unmarshal(body, &got)
	data := got["data"].(map[string]any)
	if data["id"] != "ws-abc" {
		t.Errorf("id missing: %s", body)
	}
}

func TestParseResource(t *testing.T) {
	in := []byte(`{"data":{"id":"ws-abc","type":"workspaces","attributes":{"name":"api-prod","auto-apply":true}}}`)
	r, err := ParseResource(in)
	if err != nil {
		t.Fatal(err)
	}
	if r.ID != "ws-abc" || r.Type != "workspaces" {
		t.Errorf("ParseResource = %+v", r)
	}
	if GetStringAttr(r, "name") != "api-prod" {
		t.Errorf("name attr: %q", GetStringAttr(r, "name"))
	}
	if !GetBoolAttr(r, "auto-apply") {
		t.Error("auto-apply attr")
	}
}

func TestParseResourceList(t *testing.T) {
	in := []byte(`{"data":[
	  {"id":"a","type":"workspaces","attributes":{"name":"x"}},
	  {"id":"b","type":"workspaces","attributes":{"name":"y"}}
	]}`)
	rs, err := ParseResourceList(in)
	if err != nil {
		t.Fatal(err)
	}
	if len(rs) != 2 || rs[0].ID != "a" || rs[1].ID != "b" {
		t.Errorf("list: %+v", rs)
	}
}

func TestGetAttrs_ZeroValueOnMissing(t *testing.T) {
	r := &Resource{Attributes: map[string]json.RawMessage{}}
	if GetStringAttr(r, "k") != "" {
		t.Error("missing string")
	}
	if GetBoolAttr(r, "k") != false {
		t.Error("missing bool")
	}
	if GetIntAttr(r, "k") != 0 {
		t.Error("missing int")
	}
	if GetFloat64Attr(r, "k") != 0 {
		t.Error("missing float")
	}
	if GetMapAttr(r, "k") != nil {
		t.Error("missing map should be nil")
	}
	if GetListAttr(r, "k") != nil {
		t.Error("missing list should be nil")
	}
}

func TestGetMapAttr_NilOnJSONNull(t *testing.T) {
	r := &Resource{Attributes: map[string]json.RawMessage{
		"labels": json.RawMessage("null"),
	}}
	if GetMapAttr(r, "labels") != nil {
		t.Error("null should produce nil")
	}
}

func TestGetIntAttr_HandlesNumericLiteral(t *testing.T) {
	r := &Resource{Attributes: map[string]json.RawMessage{
		"count": json.RawMessage("42"),
	}}
	if got := GetIntAttr(r, "count"); got != 42 {
		t.Errorf("count = %d", got)
	}
}

func TestGetRelationshipID(t *testing.T) {
	r := &Resource{Relationships: map[string]Relationship{
		"workspace": {Data: json.RawMessage(`{"id":"ws-x","type":"workspaces"}`)},
		"missing":   {Data: json.RawMessage(`null`)},
	}}
	if got := GetRelationshipID(r, "workspace"); got != "ws-x" {
		t.Errorf("got %q", got)
	}
	if got := GetRelationshipID(r, "missing"); got != "" {
		t.Errorf("null should yield empty: %q", got)
	}
	if got := GetRelationshipID(r, "absent"); got != "" {
		t.Errorf("absent should yield empty: %q", got)
	}
}

func TestStripAndAddPrefix(t *testing.T) {
	if got := StripPrefix("ws-abc", "ws-"); got != "abc" {
		t.Errorf("strip: %q", got)
	}
	if got := StripPrefix("plain", "ws-"); got != "plain" {
		t.Errorf("strip(no-prefix): %q", got)
	}
	if got := AddPrefix("abc", "ws-"); got != "ws-abc" {
		t.Errorf("add: %q", got)
	}
	if got := AddPrefix("ws-abc", "ws-"); got != "ws-abc" {
		t.Errorf("add(idempotent): %q", got)
	}
}
