package mcpserver

import (
	"context"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// TestEveryToolIsAnnotated pins GHSA-3g53-5gw3-hh42.
//
// An MCP host decides whether to ask the operator before a call from the tool's
// annotations. A tool with none is indistinguishable from a read-only one, so
// ten mutating tools — including two that override a governance gate — could be
// auto-approved with no human in the loop.
//
// This is a live assertion rather than a field in the catalogue golden on
// purpose. The golden is regenerable, and its `read_only` / `destructive` are
// `omitempty`: a tool that ships with no annotation at all serialises exactly
// like one deliberately annotated non-destructive, so the snapshot would accept
// the bug and then freeze it. The only way to see the difference is to ask the
// running server, which is what this does.
func TestEveryToolIsAnnotated(t *testing.T) {
	srv, _, err := New(Config{Host: "example.test", Name: "terrapod-test", Token: "test-token"})
	if err != nil {
		t.Fatalf("build server: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	clientT, serverT := mcp.NewInMemoryTransports()
	go func() { _ = srv.Run(ctx, serverT) }()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	sess, err := client.Connect(ctx, clientT, nil)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer func() { _ = sess.Close() }()

	res, err := sess.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("list tools: %v", err)
	}
	if len(res.Tools) == 0 {
		t.Fatal("no tools registered — this guard would pass vacuously")
	}

	for _, tool := range res.Tools {
		if tool.Annotations == nil {
			t.Errorf("%s has no annotations: a host cannot tell it from a read-only tool, "+
				"so it may be called without asking the operator. Mark it readOnly, "+
				"mutating, or destructive.", tool.Name)
			continue
		}
		a := tool.Annotations
		// A tool must say one thing or the other. ReadOnlyHint false with no
		// DestructiveHint is the same silence in a different shape.
		if !a.ReadOnlyHint && a.DestructiveHint == nil {
			t.Errorf("%s is not read-only and states no DestructiveHint; "+
				"say whether it is destructive.", tool.Name)
		}
		// Read-only and destructive together is a contradiction a host cannot
		// resolve, and it would most likely be read as the safer of the two.
		if a.ReadOnlyHint && a.DestructiveHint != nil && *a.DestructiveHint {
			t.Errorf("%s is annotated both read-only and destructive", tool.Name)
		}
	}
}
