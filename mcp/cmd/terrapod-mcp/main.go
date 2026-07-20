// Command terrapod-mcp is the Terrapod MCP server — a local stdio adapter that
// lets an MCP-capable agent (Claude, Cursor, …) drive one Terrapod instance
// through curated, RBAC-checked tools.
//
// It runs on the workstation, spawned by the agent over stdio, and is an
// ordinary API client: outbound HTTPS, authenticated with the user's
// `tofu login` token, holding no privileged access. Bind one server per
// instance (a friendly --name + --host); MCP clients namespace tools per
// server, so a dev-bound server literally cannot touch prod.
//
// Auth resolves in order: --token, then $TERRAPOD_TOKEN, then
// ~/.terraform.d/credentials.tfrc.json for --host (i.e. `tofu login <host>`).
package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/mattrobinsonsre/terrapod/mcp/internal/mcpserver"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func main() {
	fs := flag.NewFlagSet("terrapod-mcp", flag.ExitOnError)
	host := fs.String("host", "", "Terrapod instance hostname, e.g. terrapod.example.com (required)")
	name := fs.String("name", "", "friendly server name for this instance, e.g. terrapod-prod (default: terrapod)")
	token := fs.String("token", "", "API token (else $TERRAPOD_TOKEN, else ~/.terraform.d/credentials.tfrc.json for --host)")
	envHint := fs.String("env-hint", "", "optional environment label (prod|dev) — makes destructive-op guidance louder")
	skipTLS := fs.Bool("skip-tls-verify", false, "skip TLS verification (local/dev only)")
	showVersion := fs.Bool("version", false, "print version and exit")
	_ = fs.Parse(os.Args[1:])

	if *showVersion {
		fmt.Println(mcpserver.Version)
		return
	}
	if *host == "" {
		fmt.Fprintln(os.Stderr, "terrapod-mcp: --host is required")
		os.Exit(2)
	}

	tok, err := mcpserver.ResolveToken(*token, *host)
	if err != nil {
		fmt.Fprintln(os.Stderr, "terrapod-mcp:", err)
		os.Exit(1)
	}
	// A token from the credentials file can be refreshed live on a 401 (a
	// `tofu login` re-write); an explicit --token / $TERRAPOD_TOKEN is static.
	refreshFromFile := *token == "" && os.Getenv("TERRAPOD_TOKEN") == ""

	srv, client, err := mcpserver.New(mcpserver.Config{
		Host:                 *host,
		Name:                 *name,
		Token:                tok,
		EnvHint:              *envHint,
		SkipTLSVerify:        *skipTLS,
		RefreshTokenFromFile: refreshFromFile,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "terrapod-mcp:", err)
		os.Exit(1)
	}

	ctx := context.Background()

	// Version negotiation — warn (to stderr; stdout is the JSON-RPC channel)
	// when this MCP is built against an incompatible API version. Non-fatal.
	if warn := mcpserver.CheckVersion(ctx, client); warn != "" {
		mcpserver.Logf("%s", warn)
	}

	mcpserver.Logf("serving %s over stdio", *host)
	if err := srv.Run(ctx, &mcp.StdioTransport{}); err != nil {
		fmt.Fprintln(os.Stderr, "terrapod-mcp:", err)
		os.Exit(1)
	}
}
