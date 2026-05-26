package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/framework"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/sources/atlantis"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/sources/tfe"
	"github.com/mattrobinsonsre/terrapod/migrate/internal/writer"
)

// applyCmd is the apply subcommand: read from a source platform,
// write to Terrapod. Default is dry-run; pass --apply to write.
//
// The flag surface is deliberately narrow on the first cut. Sources
// that need richer config (TFE org + token, Atlantis multi-repo
// scanning) layer their own flags inside this function rather than
// inflating the shared surface.
func applyCmd(args []string) int {
	fs := flag.NewFlagSet("apply", flag.ContinueOnError)
	var (
		source       = fs.String("source", "", "Source platform: 'atlantis' or 'tfe' (required)")
		sourceDir    = fs.String("source-dir", "", "Local atlantis-repo clone (required when --source=atlantis)")
		atlantisYAML = fs.String("atlantis-yaml-path", "", "Override path to atlantis.yaml (default: <source-dir>/atlantis.yaml)")
		tfeAddress   = fs.String("tfe-address", os.Getenv("TFE_ADDRESS"), "TFE API address (or TFE_ADDRESS; default: https://app.terraform.io)")
		tfeToken     = fs.String("tfe-token", os.Getenv("TFE_TOKEN"), "TFE API token (or TFE_TOKEN; org-owner preferred for sensitive-variable visibility)")
		tfeOrg       = fs.String("tfe-org", os.Getenv("TFE_ORG"), "TFE organisation to migrate (or TFE_ORG)")
		target       = fs.String("target", os.Getenv("TERRAPOD_HOSTNAME"), "Terrapod base URL (or TERRAPOD_HOSTNAME)")
		token        = fs.String("token", os.Getenv("TERRAPOD_TOKEN"), "Terrapod API token (or TERRAPOD_TOKEN)")
		statePath    = fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
		apply        = fs.Bool("apply", false, "Actually write to Terrapod (default is dry-run)")
		jsonReport   = fs.Bool("json", false, "Emit the final Report as JSON instead of a text summary")
		skipTLS      = fs.Bool("skip-tls-verify", false, "Skip TLS certificate verification (dev only)")
	)
	if err := fs.Parse(args); err != nil {
		// flag.ContinueOnError already printed the usage; bail.
		return 2
	}

	if *source == "" {
		fmt.Fprintln(os.Stderr, "apply: --source is required (atlantis|tfe)")
		fs.Usage()
		return 2
	}
	if *target == "" {
		fmt.Fprintln(os.Stderr, "apply: --target (or TERRAPOD_HOSTNAME) is required")
		return 2
	}
	if *token == "" {
		fmt.Fprintln(os.Stderr, "apply: --token (or TERRAPOD_TOKEN) is required")
		return 2
	}

	// Build the IR Plan from the source-specific loader.
	var (
		plan        ir.Plan
		credsByConn map[string]writer.Creds
		err         error
	)
	switch *source {
	case "atlantis":
		if *sourceDir == "" {
			fmt.Fprintln(os.Stderr, "apply: --source-dir is required for --source=atlantis")
			return 2
		}
		plan, credsByConn, err = loadAtlantisPlan(*sourceDir, *atlantisYAML)
	case "tfe":
		plan, credsByConn, err = loadTFEPlan(context.Background(), *tfeAddress, *tfeToken, *tfeOrg)
	default:
		fmt.Fprintf(os.Stderr, "apply: unknown --source %q (atlantis|tfe)\n", *source)
		return 2
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: load %s source: %v\n", *source, err)
		return 1
	}

	// Load or initialise the migration state file. Loading is best-
	// effort — a non-existent file is the normal first-run case.
	state, err := framework.Load(*statePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		state = &framework.State{}
	}

	// Build the SDK client. Note: in dry-run mode the writer never
	// actually calls the client (its DryRun branch short-circuits
	// before any HTTP), so an unreachable --target is fine for
	// dry-run inspection of the Plan.
	c, err := terrapod.NewClient(terrapod.Options{
		BaseURL:       *target,
		Token:         *token,
		SkipTLSVerify: *skipTLS,
		UserAgent:     "terrapod-migrate/" + Version,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: build terrapod client: %v\n", err)
		return 1
	}

	w := writer.New(c, state, *statePath)
	opts := writer.Options{
		DryRun:      !*apply,
		ToolVersion: Version,
		CredsForVCSConnection: func(conn *ir.VCSConnection) (writer.Creds, error) {
			if creds, ok := credsByConn[conn.SourceID]; ok {
				return creds, nil
			}
			return writer.Creds{}, fmt.Errorf("no credentials available for vcs-connection %q (provide via the source plugin)", conn.SourceID)
		},
		SensitiveValueForVariable: func(workspaceSourceID, key string) (string, error) {
			return "", fmt.Errorf("sensitive variable %q on workspace %q: source did not provide a value-loader (atlantis has no sensitive vars; tfe support pending)", key, workspaceSourceID)
		},
	}

	report, err := w.Run(context.Background(), plan, opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: writer aborted: %v\n", err)
		return 1
	}

	if *jsonReport {
		if data, err := json.MarshalIndent(report, "", "  "); err == nil {
			fmt.Println(string(data))
		}
	} else {
		printReportSummary(report, !*apply)
	}

	if len(report.Errors) > 0 {
		return 1
	}
	return 0
}

// loadAtlantisPlan reads a local atlantis clone, parses its
// atlantis.yaml, and assembles the IR Plan plus the per-connection
// credentials map (Atlantis migrations get one VCS connection per
// repo URL, and the credential payload is configured by the
// operator's VCS provider — for the first cut we use a stub since
// Atlantis itself doesn't carry GitHub-App credentials we could
// inherit).
//
// Credentials note: Atlantis has no notion of API-level repo
// credentials embedded in atlantis.yaml. The operator brings their
// own GitHub App / GitLab PAT, set via environment variables matched
// to the VCSConnection.SourceID. This is a minimal pass-through so
// dry-run reporting works; --apply on atlantis migrations requires
// operators to set TERRAPOD_MIGRATE_GITHUB_APP_ID,
// TERRAPOD_MIGRATE_GITHUB_INSTALLATION_ID, and
// TERRAPOD_MIGRATE_GITHUB_PRIVATE_KEY before running. The
// integration tests and pre-release smoke will exercise the env-var
// path; today's CLI just emits a friendly error if --apply is set
// without them.
func loadAtlantisPlan(sourceDir, yamlPath string) (ir.Plan, map[string]writer.Creds, error) {
	src, err := atlantis.LoadDirectory(sourceDir, atlantis.LoadOptions{
		AtlantisYAMLPath: yamlPath,
	})
	if err != nil {
		return ir.Plan{}, nil, err
	}

	connSourceID := atlantisConnSourceID(src.RepoURL)
	workspaces, skipped, err := atlantis.Emit(src.AtlantisYAML, atlantis.EmitOptions{
		Repo:             src.RepoURL,
		VCSConnectionRef: connSourceID,
		DefaultBranch:    src.DefaultBranch,
	})
	if err != nil {
		return ir.Plan{}, nil, err
	}

	plan := ir.Plan{
		Source: "atlantis",
		SourceMetadata: map[string]string{
			"host":       hostFromRepoURL(src.RepoURL),
			"repo_url":   src.RepoURL,
			"clone_path": src.SourcePath,
		},
		VCSConnections: []ir.VCSConnection{
			{
				SourceID: connSourceID,
				Name:     "atlantis-" + hostFromRepoURL(src.RepoURL),
				Provider: providerFromRepoURL(src.RepoURL),
			},
		},
		Workspaces: workspaces,
		Skipped:    skipped,
	}

	creds := map[string]writer.Creds{
		connSourceID: credsFromEnv(providerFromRepoURL(src.RepoURL)),
	}
	return plan, creds, nil
}

// loadTFEPlan connects to a TFE/HCP instance and assembles the IR
// Plan. Sensitive variable values are NOT inlined here — the writer
// loads them lazily via the SensitiveValueForVariable callback only
// in --apply mode, which keeps dry-run runs from making redundant
// API calls and keeps sensitive payloads out of the dry-run report.
//
// Credentials for VCS connections are out of scope for this first
// TFE wiring: TFE migrations carry over the workspace's
// vcs_repo_url as metadata, but the operator wires up the matching
// GitHub App / GitLab PAT separately (same env-var convention as
// the atlantis flow). Future work: read TFE's oauth-client list and
// surface them in the report so operators know which connections
// to recreate.
func loadTFEPlan(ctx context.Context, address, token, org string) (ir.Plan, map[string]writer.Creds, error) {
	c, err := tfe.NewClient(ctx, tfe.Config{
		Address: address,
		Token:   token,
		OrgName: org,
	})
	if err != nil {
		return ir.Plan{}, nil, err
	}

	workspaces, conns, skipped, err := c.EmitWorkspaces(ctx)
	if err != nil {
		return ir.Plan{}, nil, err
	}
	varSkipped, err := c.AttachVariables(ctx, workspaces)
	if err != nil {
		return ir.Plan{}, nil, err
	}
	skipped = append(skipped, varSkipped...)

	plan := ir.Plan{
		Source: "tfe",
		SourceMetadata: map[string]string{
			"host":  hostFromRepoURL(c.Address),
			"org":   c.OrgName,
			"token": string(c.TokenTier),
		},
		VCSConnections: conns,
		Workspaces:     workspaces,
		Skipped:        skipped,
	}

	// Per-connection creds map: empty for now. Operators wire VCS
	// credentials via the same env-var convention the atlantis flow
	// uses (see credsFromEnv); the writer surfaces a clear error in
	// --apply mode if the values are missing.
	creds := map[string]writer.Creds{}
	for _, conn := range conns {
		creds[conn.SourceID] = credsFromEnv(conn.Provider)
	}
	return plan, creds, nil
}

// printReportSummary prints a human-readable summary of the writer's
// report. JSON output is available via --json for tooling.
func printReportSummary(r *writer.Report, dryRun bool) {
	label := "applied"
	if dryRun {
		label = "planned (dry-run; pass --apply to write)"
	}
	fmt.Printf("\nterrapod-migrate apply — %s\n", label)
	fmt.Printf("  source:        %s\n", r.Source)
	fmt.Printf("  started:       %s\n", r.StartedAt.Format("2006-01-02 15:04:05"))
	if !r.FinishedAt.IsZero() {
		fmt.Printf("  finished:      %s\n", r.FinishedAt.Format("2006-01-02 15:04:05"))
	}
	fmt.Printf("  connections:   %d\n", len(r.Connections))
	fmt.Printf("  workspaces:    %d\n", len(r.Workspaces))
	fmt.Printf("  skipped:       %d\n", len(r.Skipped))
	if len(r.Errors) > 0 {
		fmt.Printf("  errors:        %d\n", len(r.Errors))
		for _, e := range r.Errors {
			fmt.Printf("    - %s\n", e)
		}
	}
	if len(r.Skipped) > 0 {
		fmt.Println("\n  skipped items (operator action required):")
		for _, s := range r.Skipped {
			fmt.Printf("    - %s %q: %s\n", s.Kind, s.Name, s.Reason)
		}
	}
}

// statusCmd prints the contents of the migration state file. Used by
// operators to audit progress between apply runs or to confirm a
// rewrite subcommand will read what they expect.
func statusCmd(args []string) int {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	statePath := fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	state, err := framework.Load(*statePath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(os.Stderr, "status: state file %s not found (run `apply` first)\n", *statePath)
			return 1
		}
		fmt.Fprintf(os.Stderr, "status: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		fmt.Fprintf(os.Stderr, "status: state file %s not found (run `apply` first)\n", *statePath)
		return 1
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "status: marshal state: %v\n", err)
		return 1
	}
	fmt.Println(string(data))
	return 0
}

// ── Helpers ──────────────────────────────────────────────────────────

func atlantisConnSourceID(repoURL string) string {
	// Stable per-repo id so re-running apply against the same clone
	// resolves to the same VCS connection record.
	return "atlantis-vcs:" + repoURL
}

func hostFromRepoURL(repoURL string) string {
	for _, prefix := range []string{"https://", "http://", "ssh://", "git@"} {
		if len(repoURL) > len(prefix) && repoURL[:len(prefix)] == prefix {
			repoURL = repoURL[len(prefix):]
		}
	}
	for i := 0; i < len(repoURL); i++ {
		if repoURL[i] == '/' || repoURL[i] == ':' {
			return repoURL[:i]
		}
	}
	return repoURL
}

func providerFromRepoURL(repoURL string) string {
	host := hostFromRepoURL(repoURL)
	switch {
	case host == "github.com":
		return "github"
	case host == "gitlab.com":
		return "gitlab"
	case len(host) >= 7 && host[len(host)-7:] == "gitlab.": // self-hosted-ish
		return "gitlab"
	default:
		// Self-hosted: best-guess from the host hint. Operators
		// override by setting TERRAPOD_MIGRATE_VCS_PROVIDER.
		if env := os.Getenv("TERRAPOD_MIGRATE_VCS_PROVIDER"); env != "" {
			return env
		}
		return "github"
	}
}

func credsFromEnv(provider string) writer.Creds {
	switch provider {
	case "github":
		// Atlantis-side migrations don't carry GitHub-App credentials
		// in atlantis.yaml; operators wire them through environment.
		// We don't validate here — Apply will surface a clear error
		// from the API if the credentials are wrong.
		return writer.Creds{
			PrivateKey: os.Getenv("TERRAPOD_MIGRATE_GITHUB_PRIVATE_KEY"),
			// IDs come back as zero from atoi-failures, which lets
			// dry-run pass without env wiring while --apply surfaces
			// a 422 from the API.
			GithubAppID:          atoiSafe(os.Getenv("TERRAPOD_MIGRATE_GITHUB_APP_ID")),
			GithubInstallationID: atoiSafe(os.Getenv("TERRAPOD_MIGRATE_GITHUB_INSTALLATION_ID")),
		}
	case "gitlab":
		return writer.Creds{Token: os.Getenv("TERRAPOD_MIGRATE_GITLAB_TOKEN")}
	}
	return writer.Creds{}
}

func atoiSafe(s string) int64 {
	var n int64
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0
		}
		n = n*10 + int64(c-'0')
	}
	return n
}
