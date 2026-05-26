package framework

import (
	"bytes"
	"fmt"
	"sort"
	"strings"
	"time"
)

// RenderHandoverMarkdown produces the post-migration handover doc as
// Markdown. The doc is written for the team taking over the migrated
// workspaces — it lists every workspace, its old vs new identifier,
// any skipped items the operator must handle by hand, and the cutover
// runbook commands. Saved next to the migration state file by the
// apply subcommand so it's discoverable in CI/code review.
//
// The output is deterministic — same inputs always produce the same
// bytes — so the doc can be checked into version control and diffed
// across runs.
func RenderHandoverMarkdown(s *State) []byte {
	var buf bytes.Buffer
	w := func(format string, args ...any) {
		fmt.Fprintf(&buf, format+"\n", args...)
	}
	w("# Terrapod Migration Handover")
	w("")
	if s.UpdatedAt.IsZero() {
		w("_Generated %s_", time.Now().UTC().Format("2006-01-02 15:04 UTC"))
	} else {
		w("_Migration state last updated %s_", s.UpdatedAt.UTC().Format("2006-01-02 15:04 UTC"))
	}
	w("")
	w("- **Source platform:** `%s`", emptyDash(s.Source))
	if s.SourceHost != "" {
		w("- **Source host:** `%s`", s.SourceHost)
	}
	if s.SourceOrg != "" {
		w("- **Source org:** `%s`", s.SourceOrg)
	}
	w("- **Destination:** `%s`", emptyDash(s.DestHost))
	w("- **Tool version:** `%s`", emptyDash(s.ToolVersion))
	w("")

	w("## Workspaces (%d)", len(s.Workspaces))
	w("")
	if len(s.Workspaces) > 0 {
		// Deterministic sort by source name.
		rows := append([]WorkspaceRecord(nil), s.Workspaces...)
		sort.Slice(rows, func(i, j int) bool { return rows[i].SourceName < rows[j].SourceName })

		w("| Source name | Terrapod ID | State | Serial | Lineage |")
		w("|-------------|-------------|-------|-------:|---------|")
		for _, ws := range rows {
			lineage := ws.StateLineage
			if len(lineage) > 8 {
				lineage = lineage[:8] + "…"
			}
			w("| `%s` | `%s` | %s | %d | %s |",
				ws.SourceName,
				emptyDash(ws.TerrapodID),
				emptyDash(ws.State),
				ws.StateSerial,
				emptyDash(lineage),
			)
		}
		w("")
	}

	if len(s.VCSConnections) > 0 {
		w("## VCS Connections (%d)", len(s.VCSConnections))
		w("")
		w("| Name | Provider | Terrapod ID | State |")
		w("|------|----------|-------------|-------|")
		conns := append([]VCSConnectionRecord(nil), s.VCSConnections...)
		sort.Slice(conns, func(i, j int) bool { return conns[i].Name < conns[j].Name })
		for _, c := range conns {
			w("| `%s` | %s | `%s` | %s |",
				c.Name, emptyDash(c.Provider), emptyDash(c.TerrapodID), emptyDash(c.State))
		}
		w("")
	}

	if len(s.SkippedItems) > 0 {
		w("## Skipped — Manual Action Required (%d)", len(s.SkippedItems))
		w("")
		w("These items did not migrate automatically. Each requires operator follow-up.")
		w("")
		// Group by kind for readability.
		byKind := map[string][]SkippedRecord{}
		for _, s := range s.SkippedItems {
			byKind[s.Kind] = append(byKind[s.Kind], s)
		}
		kinds := make([]string, 0, len(byKind))
		for k := range byKind {
			kinds = append(kinds, k)
		}
		sort.Strings(kinds)
		for _, k := range kinds {
			w("### %s", k)
			w("")
			for _, item := range byKind[k] {
				w("- **%s** — %s", item.Name, item.Reason)
			}
			w("")
		}
	}

	w("## Cutover Checklist")
	w("")
	w("Run these in order; each step is idempotent.")
	w("")
	w("1. **Lock source workspaces** (TFE only): `terrapod-migrate cutover --lock --state-file %s`", DefaultStateFile)
	w("   - This prevents the source from accepting new applies during the cutover window.")
	w("2. **Re-run `apply`** to flush any state version that landed between locking and now.")
	w("3. **Rewrite operator repos** to point at Terrapod:")
	w("   ```")
	w("   terrapod-migrate rewrite --dir <repo-checkout>")
	w("   terrapod-migrate rewrite --dir <repo-checkout> --write")
	w("   ```")
	w("4. **Open a PR** in each rewritten repo. Get the standard review.")
	w("5. **Merge** to land the Terrapod-pointing HCL on the default branch.")
	w("6. **Verify** Terrapod still holds the same state:")
	w("   ```")
	w("   terrapod-migrate verify --target %s",
		emptyDash(s.DestHost))
	w("   ```")
	w("7. **Communicate** the new Terrapod URL to consumers. Decommission the source ")
	w("   workspaces only after a soak period (typically 30 days).")
	w("")
	w("To roll back: `terrapod-migrate cutover --unlock --state-file %s` and ", DefaultStateFile)
	w("delete the Terrapod-side workspaces by hand.")
	w("")

	return buf.Bytes()
}

func emptyDash(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "—"
	}
	return s
}
