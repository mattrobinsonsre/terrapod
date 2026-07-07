package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"

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
// Authentication scope is intentionally narrow:
//   - Source-side reads use go-tfe (TFE/HCP) or local git clones
//     (Atlantis). The migrator NEVER authenticates against GitHub or
//     GitLab — operators create Terrapod-side VCS connections via UI
//     or terraform-provider-terrapod, and the migrator discovers
//     existing connections to wire to migrated workspaces.
//   - State reads use the AWS / GCP / Azure SDKs' default credential
//     chains (env, profiles, IAM roles, IRSA, ADC, AZ login).
//   - Terrapod writes use the supplied --token.
func applyCmd(args []string) int {
	fs := flag.NewFlagSet("apply", flag.ContinueOnError)
	var (
		source       = fs.String("source", "", "Source platform: 'atlantis' or 'tfe' (required)")
		sourceDir    = fs.String("source-dir", "", "Local atlantis-repo clone (required when --source=atlantis)")
		atlantisYAML = fs.String("atlantis-yaml-path", "", "Override path to atlantis.yaml (default: <source-dir>/atlantis.yaml)")
		workspace    = fs.String("workspace", "", "Target an existing Terrapod workspace directly (state-push only, no atlantis.yaml needed)")
		tfeAddress   = fs.String("tfe-address", os.Getenv("TFE_ADDRESS"), "TFE API address (or TFE_ADDRESS; default: https://app.terraform.io)")
		tfeToken     = fs.String("tfe-token", os.Getenv("TFE_TOKEN"), "TFE API token (or TFE_TOKEN; org-owner preferred for sensitive-variable visibility)")
		tfeOrg       = fs.String("tfe-org", os.Getenv("TFE_ORG"), "TFE organisation to migrate (or TFE_ORG)")
		skipState    = fs.Bool("skip-state", false, "Don't migrate state — workspaces are created but state is left for operator")
		// S3 client configuration. Auth comes entirely from
		// aws-sdk-go-v2's default credential chain (env vars,
		// ~/.aws/credentials with AWS_PROFILE, AWS SSO, IAM roles,
		// IRSA, EC2/ECS instance metadata) — same chain every other
		// AWS-aware tool uses. We do not reinvent it.
		s3Endpoint  = fs.String("s3-endpoint-url", os.Getenv("AWS_ENDPOINT_URL_S3"), "S3 endpoint override (e.g. http://localhost:9000 for minio; or AWS_ENDPOINT_URL_S3)")
		s3PathStyle = fs.Bool("s3-force-path-style", os.Getenv("AWS_S3_FORCE_PATH_STYLE") == "true", "Use S3 path-style addressing (required for minio)")
		s3Region    = fs.String("s3-region", "", "S3 region override (default: AWS_REGION env, or read from backend HCL)")
		target      = fs.String("target", os.Getenv("TERRAPOD_HOSTNAME"), "Terrapod base URL (or TERRAPOD_HOSTNAME)")
		token       = fs.String("token", os.Getenv("TERRAPOD_TOKEN"), "Terrapod API token (or TERRAPOD_TOKEN)")
		statePath   = fs.String("state-file", framework.DefaultStateFile, "Path to the migration state JSON file")
		apply       = fs.Bool("apply", false, "Actually write to Terrapod (default is dry-run)")
		jsonReport  = fs.Bool("json", false, "Emit the final Report as JSON instead of a text summary")
		skipTLS     = fs.Bool("skip-tls-verify", false, "Skip TLS certificate verification (dev only)")
	)
	if err := fs.Parse(args); err != nil {
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

	// Build the IR Plan + a source-specific StateReader.
	var (
		plan        ir.Plan
		stateReader writer.StateReader
		err         error
	)
	switch *source {
	case "atlantis":
		if *sourceDir == "" {
			fmt.Fprintln(os.Stderr, "apply: --source-dir is required for --source=atlantis")
			return 2
		}
		if *workspace != "" {
			// Direct state-push mode: target an existing workspace,
			// skip atlantis.yaml parsing entirely. The workspace must
			// already exist in Terrapod (created via UI, API, or
			// autodiscovery). We just detect the backend from HCL in
			// source-dir and push the state.
			plan, stateReader, err = loadDirectWorkspacePlan(*sourceDir, *workspace, atlantis.StateOptions{
				S3Endpoint:       *s3Endpoint,
				S3ForcePathStyle: *s3PathStyle,
				S3Region:         *s3Region,
			})
		} else {
			plan, stateReader, err = loadAtlantisPlan(*sourceDir, *atlantisYAML, atlantis.StateOptions{
				S3Endpoint:       *s3Endpoint,
				S3ForcePathStyle: *s3PathStyle,
				S3Region:         *s3Region,
			})
		}
	case "tfe":
		plan, stateReader, err = loadTFEPlan(context.Background(), *tfeAddress, *tfeToken, *tfeOrg)
	default:
		fmt.Fprintf(os.Stderr, "apply: unknown --source %q (atlantis|tfe)\n", *source)
		return 2
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: load %s source: %v\n", *source, err)
		return 1
	}
	if *skipState {
		stateReader = nil
	}

	state, err := framework.Load(*statePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "apply: load state file %s: %v\n", *statePath, err)
		return 1
	}
	if state == nil {
		state = &framework.State{}
	}

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

	// --workspace mode: pre-seed the state file with the existing
	// workspace's Terrapod ID so the writer takes the "reused" path
	// (state push only, no create attempt).
	if *workspace != "" {
		sourceID := "direct:" + *workspace
		if state.WorkspaceBySourceID(sourceID) == nil {
			existing, lookupErr := c.GetWorkspaceByName(context.Background(), *workspace)
			if lookupErr != nil {
				fmt.Fprintf(os.Stderr, "apply: --workspace %q not found in Terrapod: %v\n", *workspace, lookupErr)
				return 1
			}
			state.Workspaces = append(state.Workspaces, framework.WorkspaceRecord{
				SourceID:   sourceID,
				SourceName: *workspace,
				TerrapodID: existing.ID,
				State:      "created",
			})
		}
	}

	// Resolve plan VCS connections to Terrapod-side connection IDs by
	// listing existing connections and matching on server URL +
	// provider. The migrator never creates connections — operators
	// wire them up once via UI/provider, and we discover them here.
	// In dry-run mode we still attempt the lookup so the report
	// shows exactly which connections are wired and which need
	// operator follow-up.
	connByRef, err := resolveVCSConnections(context.Background(), c, plan.VCSConnections)
	if err != nil && *apply {
		// In --apply mode a Terrapod-side lookup failure is fatal —
		// we'd otherwise wire every workspace without a VCS link.
		// In dry-run we proceed with an empty map so the operator
		// sees the planned "missing connection" surface.
		fmt.Fprintf(os.Stderr, "apply: list Terrapod VCS connections: %v\n", err)
		return 1
	}

	w := writer.New(c, state, *statePath)
	opts := writer.Options{
		DryRun:               !*apply,
		ToolVersion:          Version,
		StateForWorkspace:    stateReader,
		VCSConnectionIDByRef: connByRef,
		DestHost:             hostFromRepoURL(*target),
		// SensitiveValueForVariable left nil — the writer creates
		// sensitive variables with empty values + sensitive=true,
		// so they appear in the destination workspace UI for the
		// operator to fill in post-cutover. The callback is reserved
		// for future value-loader plugins (Vault, sops, etc.).
		SensitiveValueForVariable: nil,
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

// resolveVCSConnections lists the Terrapod-side VCS connections and
// builds a SourceID → TerrapodID map by matching each plan
// connection against existing Terrapod connections. Match rules:
//
//   - exact server URL match wins (case-insensitive)
//   - falling back to host equality (strip scheme + port + path)
//   - provider must match (github / gitlab)
//
// A nil/empty return is valid — the writer treats unmatched
// connections as Skipped items in the report.
func resolveVCSConnections(ctx context.Context, c *terrapod.Client, planConns []ir.VCSConnection) (map[string]string, error) {
	out := map[string]string{}
	if len(planConns) == 0 {
		return out, nil
	}
	existing, err := c.ListVCSConnections(ctx)
	if err != nil {
		return nil, err
	}
	for _, planConn := range planConns {
		want := canonicaliseURL(planConn.ServerURL)
		wantHost := canonicaliseGitHost(planConn.ServerURL)
		for i := range existing {
			ex := &existing[i]
			if !strings.EqualFold(ex.Provider, planConn.Provider) {
				continue
			}
			exHost := canonicaliseGitHost(ex.ServerURL)
			if canonicaliseURL(ex.ServerURL) == want ||
				(want == "" && ex.ServerURL == "") || // default-host on both sides
				exHost == wantHost {
				out[planConn.SourceID] = ex.ID
				break
			}
		}
	}
	return out, nil
}

// canonicaliseGitHost collapses common shape differences between
// Terrapod-side and source-side hostnames so the connection-matcher
// hits even when only one side declares `api.` (GitHub's REST API
// host is api.github.com but operators clone from github.com) or
// includes a non-standard port.
func canonicaliseGitHost(serverURL string) string {
	host := hostFromRepoURL(serverURL)
	// Strip a leading "api." since the GitHub App config commonly
	// has `server_url: https://api.github.com` but repo URLs use
	// `github.com`. GitLab self-hosted instances commonly use the
	// same host for API + repos, so this only matters for GitHub.
	host = strings.TrimPrefix(host, "api.")
	return strings.ToLower(host)
}

func canonicaliseURL(u string) string {
	u = strings.ToLower(strings.TrimSpace(u))
	u = strings.TrimSuffix(u, "/")
	return u
}

func loadAtlantisPlan(sourceDir, yamlPath string, stateOpts atlantis.StateOptions) (ir.Plan, writer.StateReader, error) {
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
				SourceID:  connSourceID,
				Name:      "atlantis-" + hostFromRepoURL(src.RepoURL),
				Provider:  providerFromRepoURL(src.RepoURL),
				ServerURL: "https://" + hostFromRepoURL(src.RepoURL),
			},
		},
		Workspaces: workspaces,
		Skipped:    skipped,
	}
	return plan, src.StateReader(stateOpts), nil
}

// loadDirectWorkspacePlan builds a minimal Plan targeting a single,
// already-existing Terrapod workspace. No atlantis.yaml needed — the
// tool detects the backend from HCL in sourceDir and provides a state
// reader. The workspace must already exist in Terrapod; the writer
// pushes state to it by name match.
func loadDirectWorkspacePlan(sourceDir, workspaceName string, stateOpts atlantis.StateOptions) (ir.Plan, writer.StateReader, error) {
	plan := ir.Plan{
		Source: "atlantis",
		SourceMetadata: map[string]string{
			"mode":       "direct-workspace",
			"clone_path": sourceDir,
			"workspace":  workspaceName,
		},
		Workspaces: []ir.Workspace{
			{
				SourceID: "direct:" + workspaceName,
				Name:     workspaceName,
			},
		},
	}

	stateReader := func(ctx context.Context, _ string) ([]byte, string, int64, error) {
		return atlantis.ReadStateFromDir(ctx, sourceDir, stateOpts)
	}

	return plan, stateReader, nil
}

func loadTFEPlan(ctx context.Context, address, token, org string) (ir.Plan, writer.StateReader, error) {
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

	varsets, vsSkipped, err := c.VariableSets(ctx)
	if err != nil {
		return ir.Plan{}, nil, err
	}
	skipped = append(skipped, vsSkipped...)

	runTriggers, err := c.RunTriggers(ctx, workspaces)
	if err != nil {
		return ir.Plan{}, nil, err
	}

	notifications, ntSkipped, err := c.Notifications(ctx, workspaces)
	if err != nil {
		return ir.Plan{}, nil, err
	}
	skipped = append(skipped, ntSkipped...)

	agentPools, err := c.AgentPools(ctx)
	if err != nil {
		return ir.Plan{}, nil, err
	}

	plan := ir.Plan{
		Source: "tfe",
		SourceMetadata: map[string]string{
			"host":  hostFromRepoURL(c.Address),
			"org":   c.OrgName,
			"token": string(c.TokenTier),
		},
		VCSConnections: conns,
		Workspaces:     workspaces,
		VariableSets:   varsets,
		RunTriggers:    runTriggers,
		Notifications:  notifications,
		AgentPools:     agentPools,
		Skipped:        skipped,
	}
	return plan, c.StateReader(), nil
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
	fmt.Printf("  variable sets: %d\n", len(r.VariableSets))
	fmt.Printf("  run triggers:  %d\n", len(r.RunTriggers))
	fmt.Printf("  notifications: %d\n", len(r.Notifications))
	fmt.Printf("  agent pools:   %d\n", len(r.AgentPools))
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

	// Sensitive variables that landed on the destination as empty
	// rows need explicit operator action post-cutover. Surface them
	// distinctly from generic Skipped items so they don't get lost
	// in a wall of "skipped" output. The handover doc renders the
	// same set under "Manual Action Required".
	var needsValue []string
	for _, ws := range r.Workspaces {
		for _, v := range ws.VarOutcomes {
			if v.State == "needs_value" {
				needsValue = append(needsValue, fmt.Sprintf("%s / %s", ws.Name, v.Key))
			}
		}
	}
	for _, vs := range r.VariableSets {
		for _, v := range vs.VarOutcomes {
			if v.State == "needs_value" {
				needsValue = append(needsValue, fmt.Sprintf("varset %s / %s", vs.Name, v.Key))
			}
		}
	}
	if len(needsValue) > 0 {
		fmt.Printf("\n  sensitive variables needing operator action (%d):\n", len(needsValue))
		fmt.Println("    These were created as empty rows with sensitive=true. Open each")
		fmt.Println("    workspace/variable set in Terrapod and fill in the value before the next plan.")
		for _, n := range needsValue {
			fmt.Printf("    - %s\n", n)
		}
	}

	// Variable-set assignments that couldn't be resolved (the referenced
	// workspace wasn't in the migration scope) need a manual assignment.
	var unresolved []string
	for _, vs := range r.VariableSets {
		for _, ref := range vs.Unresolved {
			unresolved = append(unresolved, fmt.Sprintf("varset %s → source workspace %s", vs.Name, ref))
		}
	}
	if len(unresolved) > 0 {
		fmt.Printf("\n  variable-set assignments needing operator action (%d):\n", len(unresolved))
		fmt.Println("    These sets reference workspaces outside the migration scope. Assign")
		fmt.Println("    them to the intended workspaces in Terrapod by hand.")
		for _, u := range unresolved {
			fmt.Printf("    - %s\n", u)
		}
	}

	// Run triggers whose source or destination wasn't migrated can't be
	// created automatically — the operator wires them up once both
	// workspaces exist on Terrapod.
	var skippedTriggers []string
	for _, rt := range r.RunTriggers {
		if rt.State == "skipped" {
			skippedTriggers = append(skippedTriggers, fmt.Sprintf("%s → %s", rt.SourceName, rt.DestinationName))
		}
	}
	if len(skippedTriggers) > 0 {
		fmt.Printf("\n  run triggers needing operator action (%d):\n", len(skippedTriggers))
		fmt.Println("    One or both endpoints were outside the migration scope. Recreate")
		fmt.Println("    these source → destination triggers in Terrapod once both workspaces exist.")
		for _, s := range skippedTriggers {
			fmt.Printf("    - %s\n", s)
		}
	}

	// Generic-webhook notification configs migrate with an empty HMAC
	// token (the source never returns it). The operator must re-enter
	// the token before the receiver will accept signed deliveries.
	var needsToken []string
	for _, nc := range r.Notifications {
		if nc.NeedsToken && nc.State != "skipped" {
			needsToken = append(needsToken, fmt.Sprintf("%s / %s", nc.WorkspaceName, nc.Name))
		}
	}
	if len(needsToken) > 0 {
		fmt.Printf("\n  notification tokens needing operator action (%d):\n", len(needsToken))
		fmt.Println("    These generic-webhook configs were created with an empty HMAC token")
		fmt.Println("    (the source never returns it). Re-enter each token in Terrapod if the")
		fmt.Println("    receiver verifies signatures.")
		for _, n := range needsToken {
			fmt.Printf("    - %s\n", n)
		}
	}

	// Notification configs whose destination workspace wasn't migrated
	// can't be attached automatically.
	var skippedNotifications []string
	for _, nc := range r.Notifications {
		if nc.State == "skipped" {
			skippedNotifications = append(skippedNotifications, fmt.Sprintf("%s / %s", nc.WorkspaceName, nc.Name))
		}
	}
	if len(skippedNotifications) > 0 {
		fmt.Printf("\n  notifications needing operator action (%d):\n", len(skippedNotifications))
		fmt.Println("    The destination workspace was outside the migration scope. Recreate")
		fmt.Println("    these configs in Terrapod once the workspace exists.")
		for _, s := range skippedNotifications {
			fmt.Printf("    - %s\n", s)
		}
	}

	// Every migrated agent pool needs a fresh join token + redeployed
	// listeners — TFE agent tokens are write-only and never portable.
	var poolsNeedingListeners []string
	for _, ap := range r.AgentPools {
		if ap.State == "created" || ap.State == "reused" {
			poolsNeedingListeners = append(poolsNeedingListeners, ap.Name)
		}
	}
	if len(poolsNeedingListeners) > 0 {
		fmt.Printf("\n  agent pools needing operator action (%d):\n", len(poolsNeedingListeners))
		fmt.Println("    The pool + its workspace assignments were migrated, but agent tokens")
		fmt.Println("    are never portable. For each pool, generate a fresh join token in")
		fmt.Println("    Terrapod and redeploy your listeners against it before agent runs work.")
		for _, n := range poolsNeedingListeners {
			fmt.Printf("    - %s\n", n)
		}
	}

	// Pool member workspaces that weren't migrated can't be re-pointed.
	var unresolvedPoolWorkspaces []string
	for _, ap := range r.AgentPools {
		for _, ref := range ap.Unresolved {
			unresolvedPoolWorkspaces = append(unresolvedPoolWorkspaces, fmt.Sprintf("pool %s → source workspace %s", ap.Name, ref))
		}
	}
	if len(unresolvedPoolWorkspaces) > 0 {
		fmt.Printf("\n  agent-pool assignments needing operator action (%d):\n", len(unresolvedPoolWorkspaces))
		fmt.Println("    These pool member workspaces were outside the migration scope. Point")
		fmt.Println("    them at the migrated pool in Terrapod by hand once they exist.")
		for _, u := range unresolvedPoolWorkspaces {
			fmt.Printf("    - %s\n", u)
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
	case strings.HasPrefix(host, "gitlab."):
		return "gitlab"
	default:
		if env := os.Getenv("TERRAPOD_MIGRATE_VCS_PROVIDER"); env != "" {
			return env
		}
		return "github"
	}
}
