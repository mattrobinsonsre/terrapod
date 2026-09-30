// Package mcpserver builds the Terrapod MCP server: a thin, typed adapter that
// lets an MCP-capable agent (Claude, Cursor, …) drive the Terrapod API through
// curated tools. It is an ordinary API client — outbound HTTPS, the user's
// `tofu login` token, per-user capability RBAC — holding no privileged access.
//
// One server is bound to one Terrapod instance (host + token). MCP clients
// namespace tools per server, so an agent configured with a dev server and a
// prod server gets two clearly-labelled tool sets, and a server bound to dev
// literally cannot touch prod (it holds no prod token/host).
//
// # Consumer contract (see AGENTS.md → "The API ↔ Consumer contract")
//
// terrapod-mcp is a first-class, *transitive* consumer of the Terrapod API: every
// tool here is backed by a go-terrapod call and reads the JSON:API shape off
// go-terrapod — it carries no marshaling of its own. So a change to an endpoint,
// a go-terrapod method, or a JSON:API attribute that a tool surfaces (or should
// surface) is not complete until the tool + its tool_catalogue.json golden are
// updated in the same change. New agent-observable/-drivable capability is not
// done until it has a tool. Tools never widen access: they inherit the caller's
// `tofu login` RBAC, and mutating tools that remove config or infrastructure
// carry the `destructive` hint so the host confirms.
package mcpserver

import (
	"context"
	"fmt"
	"os"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Version is the MCP server version, overridden at release build time via
// -ldflags. Also handed to go-terrapod as its SDKVersion so the version
// negotiation compares against the server's advertised API version.
var Version = "dev"

// Config is the resolved runtime configuration for one bound instance.
type Config struct {
	// Host is the Terrapod instance hostname (e.g. terrapod.example.com). It is
	// both the API target and the credentials-file key.
	Host string
	// Name is a friendly label for this server (e.g. "terrapod-prod"), surfaced
	// in the instructions so the agent never confuses instances.
	Name string
	// Token is the resolved API token (see ResolveToken).
	Token string
	// EnvHint is an optional operator-set environment label ("prod"/"dev") that
	// makes destructive-op guidance louder on production.
	EnvHint string
	// SkipTLSVerify disables certificate verification (local/dev only).
	SkipTLSVerify bool
	// RefreshTokenFromFile is true when Token was resolved from the credentials
	// file (not an explicit --token / $TERRAPOD_TOKEN). It lets the client
	// re-read the file and retry once on a 401, so an in-session `tofu login`
	// refresh is picked up without reconnecting the MCP (issue #880).
	RefreshTokenFromFile bool
}

// New builds the go-terrapod client for the configured instance and an MCP
// server with the Terrapod tool set + orientation instructions registered.
func New(cfg Config) (*mcp.Server, *terrapod.Client, error) {
	if cfg.Host == "" {
		return nil, nil, fmt.Errorf("host is required")
	}
	if cfg.Token == "" {
		return nil, nil, fmt.Errorf("token is required")
	}
	// Pin go-terrapod's version to ours so its VersionCheck compares the
	// MCP-built-against version to what the server advertises.
	terrapod.SDKVersion = Version

	// Inject an http.Client that refreshes the token on a 401 (issue #880) so an
	// in-session `tofu login` refresh is picked up without reconnecting. It owns
	// the TLS config now, so SkipTLSVerify is passed to it, not to go-terrapod
	// (which ignores its own SkipTLSVerify once an HTTPClient is supplied).
	client, err := terrapod.NewClient(terrapod.Options{
		BaseURL:    cfg.Host,
		Token:      cfg.Token,
		HTTPClient: newHTTPClient(cfg.Host, cfg.Token, cfg.RefreshTokenFromFile, cfg.SkipTLSVerify),
	})
	if err != nil {
		return nil, nil, fmt.Errorf("build terrapod client: %w", err)
	}

	impl := &mcp.Implementation{
		Name:    serverName(cfg),
		Title:   fmt.Sprintf("Terrapod (%s)", cfg.Host),
		Version: Version,
	}
	srv := mcp.NewServer(impl, &mcp.ServerOptions{
		Instructions: instructions(cfg),
	})

	registerObserve(srv, client)
	registerAct(srv, client)
	registerCRUD(srv, client)
	registerGround(srv, client)
	registerDiscover(srv, client)

	return srv, client, nil
}

func serverName(cfg Config) string {
	if cfg.Name != "" {
		return cfg.Name
	}
	return "terrapod"
}

// instructions is the one-paragraph server orientation the agent reads on
// connect. It names the bound instance (so the agent never confuses
// environments), states the safety model, and — because Terrapod is
// multi-engine — tells the agent to read a workspace's or run's `engine`
// before reaching for a tool. Several tools answer for one engine only, and an
// agent that assumes every workspace is Terraform gets a confidently wrong
// answer rather than a refusal (#1911).
//
// It names OpenTofu/Terraform and Pulumi, and nothing else: Ansible is planned
// but does not ship, and orientation text that claims an engine the instance
// cannot run is worse than orientation text that is short.
func instructions(cfg Config) string {
	env := ""
	if cfg.EnvHint != "" {
		env = fmt.Sprintf(" This is a **%s** environment — be especially careful with destructive actions.", cfg.EnvHint)
	}
	return fmt.Sprintf(
		"This server drives the Terrapod instance at %s — a self-hosted platform that "+
			"ORCHESTRATES infrastructure-as-code engines rather than being one: OpenTofu and "+
			"Terraform, and Pulumi. All tools act on THIS instance only, authenticated as the "+
			"user's `tofu login` identity, so every action is bounded by that user's Terrapod "+
			"RBAC — a read-only user cannot mutate. "+
			"**Every workspace and run reports its `engine` — read it before choosing a tool.** "+
			"Not all tools answer for every engine: the structured plan JSON is OpenTofu/"+
			"Terraform's `show -json` document, where a Pulumi run writes a preview digest "+
			"instead; a Pulumi run carries no IaC security scan; and the AI architecture "+
			"critique reads Terraform state only. What a phase is CALLED is the engine's too — "+
			"Terraform plans and applies where Pulumi previews and updates — though the run "+
			"states are the same either way. "+
			"Use the read tools (workspaces, runs, plan JSON, state, drift, policy results) to "+
			"ground and diagnose before acting. Runs go through the normal gated lifecycle "+
			"(plan-only unless an apply is explicitly confirmed and the workspace allows it). "+
			"Treat delete/destroy/RBAC-change tools as irreversible and confirm with the user "+
			"first.%s",
		cfg.Host, env)
}

// CheckVersion runs go-terrapod's version negotiation and returns an
// agent-facing warning string when the MCP is built against an incompatible
// API version, or "" when compatible / on a dev build. Logged to stderr by the
// caller (stdout is the JSON-RPC channel).
func CheckVersion(ctx context.Context, client *terrapod.Client) string {
	if err := client.VersionCheck(ctx); err != nil {
		return fmt.Sprintf("terrapod-mcp: version check: %v", err)
	}
	return ""
}

// Logf writes a diagnostic line to stderr (never stdout — that carries the
// JSON-RPC protocol).
func Logf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "terrapod-mcp: "+format+"\n", args...)
}
