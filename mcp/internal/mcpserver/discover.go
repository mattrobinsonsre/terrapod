package mcpserver

import (
	"context"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// registerDiscover adds the resource-discovery ("onboard") tools — the
// tofu-native flow that brings *existing* cloud resources under Terrapod
// management: discover a provider's importable data sources (credential-less D1
// schema), run discovery over the chosen types (D2/D3), and read back the
// generated config + matching `import {}` blocks for the user to review.
//
// These tools only DISCOVER and GENERATE — they never apply an import
// themselves (that is a separate gated import-only run). They produce reviewable
// artifacts; the human decides whether to adopt them. All are bounded by the
// caller's `workspace:onboard` capability.
func registerDiscover(s *mcp.Server, c *terrapod.Client) {
	// ── terrapod_onboard_availability ────────────────────────────────
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_onboard_availability",
		Description: "Report whether the optional AI-assisted onboarding path is available on this instance (its own switch + a configured model). The deterministic discovery flow works regardless; this only affects the AI-polished config view.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, *terrapod.OnboardingAvailability, error) {
		avail, err := c.GetOnboardingAvailability(ctx)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, avail, nil
	})

	// ── terrapod_onboard_start ───────────────────────────────────────
	type onboardStartIn struct {
		WorkspaceID     string `json:"workspace_id" jsonschema:"the workspace id (ws-...) to onboard resources into"`
		Provider        string `json:"provider" jsonschema:"lowercase terraform provider name (e.g. aws)"`
		ProviderVersion string `json:"provider_version,omitempty" jsonschema:"optional provider version constraint (e.g. ~> 5.0)"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_onboard_start",
		Description: "Start a resource-discovery session for a workspace + provider. Kicks off credential-less schema discovery (which of the provider's data sources are importable). " +
			"Poll terrapod_onboard_get until status is `schema_ready`, then read the discovery surface and call terrapod_onboard_discover with the types you want. Non-destructive — nothing is imported.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in onboardStartIn) (*mcp.CallToolResult, *terrapod.OnboardingSession, error) {
		if in.WorkspaceID == "" || in.Provider == "" {
			return errText("workspace_id and provider are required"), nil, nil
		}
		sess, err := c.CreateOnboardingSession(ctx, terrapod.CreateOnboardingSessionRequest{
			WorkspaceID:     in.WorkspaceID,
			Provider:        in.Provider,
			ProviderVersion: in.ProviderVersion,
		})
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, sess, nil
	})

	// ── terrapod_onboard_list ────────────────────────────────────────
	type onboardListIn struct {
		WorkspaceID string `json:"workspace_id" jsonschema:"the workspace id (ws-...) whose discovery sessions to list"`
	}
	type onboardListOut struct {
		Count    int                          `json:"count"`
		Sessions []terrapod.OnboardingSession `json:"sessions"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name:        "terrapod_onboard_list",
		Description: "List a workspace's resource-discovery sessions (status, provider, engine). The full discovery surface is omitted here — read one session with terrapod_onboard_get for it.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in onboardListIn) (*mcp.CallToolResult, onboardListOut, error) {
		if in.WorkspaceID == "" {
			return errText("workspace_id is required"), onboardListOut{}, nil
		}
		sessions, err := c.ListOnboardingSessions(ctx, in.WorkspaceID)
		if err != nil {
			return errResult(err), onboardListOut{}, nil
		}
		return nil, onboardListOut{Count: len(sessions), Sessions: sessions}, nil
	})

	// ── terrapod_onboard_get ─────────────────────────────────────────
	type onboardGetIn struct {
		SessionID string `json:"session_id" jsonschema:"the onboarding session id"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_onboard_get",
		Description: "Get one discovery session — its status plus (when ready) the discovery surface (importable data sources), and the generated config + `import {}` blocks. " +
			"Statuses progress: discovering schema → schema_ready (surface available) → discovering (after terrapod_onboard_discover) → config_ready (generated-config + import-blocks available). Present these to the user to review before adopting.",
		Annotations: readOnly,
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in onboardGetIn) (*mcp.CallToolResult, *terrapod.OnboardingSession, error) {
		if in.SessionID == "" {
			return errText("session_id is required"), nil, nil
		}
		sess, err := c.GetOnboardingSession(ctx, in.SessionID)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, sess, nil
	})

	// ── terrapod_onboard_discover ────────────────────────────────────
	type onboardDiscoverIn struct {
		SessionID     string   `json:"session_id" jsonschema:"the schema_ready session id"`
		SelectedTypes []string `json:"selected_types" jsonschema:"a non-empty subset of the session's discovery-surface data-source types to query"`
	}
	mcp.AddTool(s, &mcp.Tool{
		Name: "terrapod_onboard_discover",
		Description: "Run discovery over the chosen data-source types for a `schema_ready` session — dispatches the discovery run that produces the generated config + `import {}` blocks. selected_types must be a non-empty subset of the session's discovery surface. " +
			"Non-destructive: it queries and generates config, it does NOT import anything. Poll terrapod_onboard_get until config_ready, then review the output with the user.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in onboardDiscoverIn) (*mcp.CallToolResult, *terrapod.OnboardingSession, error) {
		if in.SessionID == "" {
			return errText("session_id is required"), nil, nil
		}
		if len(in.SelectedTypes) == 0 {
			return errText("selected_types must be a non-empty subset of the discovery surface"), nil, nil
		}
		sess, err := c.StartOnboardingDiscovery(ctx, in.SessionID, in.SelectedTypes)
		if err != nil {
			return errResult(err), nil, nil
		}
		return nil, sess, nil
	})
}
