package tfe

import (
	"testing"

	"github.com/hashicorp/go-tfe"

	"github.com/mattrobinsonsre/terrapod/migrate/internal/ir"
)

func TestNotificationToIR_GenericWebhook_MapsAndFlagsToken(t *testing.T) {
	nc := &tfe.NotificationConfiguration{
		ID:              "nc-1",
		Name:            "ci-hook",
		DestinationType: tfe.NotificationDestinationTypeGeneric,
		URL:             "https://hooks.example/x",
		Enabled:         true,
		Triggers:        []string{"run:created", "run:completed", "run:errored"},
	}
	got, skipped, ok := notificationToIR(nc, "ws-src", "app")
	if !ok {
		t.Fatal("expected ok=true for a generic webhook")
	}
	if got.WorkspaceRef != "ws-src" || got.Name != "ci-hook" || got.DestinationType != "generic" ||
		got.URL != "https://hooks.example/x" || !got.Enabled || got.WorkspaceName != "app" {
		t.Errorf("translation: %+v", got)
	}
	want := []string{"run:created", "run:completed", "run:errored"}
	if len(got.Triggers) != len(want) {
		t.Fatalf("triggers: got %v want %v", got.Triggers, want)
	}
	for i := range want {
		if got.Triggers[i] != want[i] {
			t.Errorf("trigger[%d]: got %q want %q", i, got.Triggers[i], want[i])
		}
	}
	// Generic webhooks always flag NeedsToken (source never returns it).
	if !got.NeedsToken {
		t.Error("expected NeedsToken=true for a generic webhook")
	}
	if !hasSkippedKind(skipped, "tfe-notification-token") {
		t.Errorf("expected a token skipped-item, got %+v", skipped)
	}
}

func TestNotificationToIR_MapsAssessmentDrifted_DropsUnsupportedTriggers(t *testing.T) {
	nc := &tfe.NotificationConfiguration{
		ID:              "nc-2",
		Name:            "slack",
		DestinationType: tfe.NotificationDestinationTypeSlack,
		URL:             "https://hooks.slack/x",
		Triggers: []string{
			"run:completed",
			"assessment:drifted",     // → run:drift_detected
			"assessment:failed",      // dropped
			"change_request:created", // dropped
		},
	}
	got, skipped, ok := notificationToIR(nc, "ws-src", "app")
	if !ok {
		t.Fatal("expected ok=true for a slack config")
	}
	want := []string{"run:completed", "run:drift_detected"}
	if len(got.Triggers) != len(want) {
		t.Fatalf("triggers: got %v want %v", got.Triggers, want)
	}
	for i := range want {
		if got.Triggers[i] != want[i] {
			t.Errorf("trigger[%d]: got %q want %q", i, got.Triggers[i], want[i])
		}
	}
	// Slack is not generic → no token flag.
	if got.NeedsToken {
		t.Error("slack config should not flag NeedsToken")
	}
	if !hasSkippedKind(skipped, "tfe-notification-trigger") {
		t.Errorf("expected a dropped-trigger skipped-item, got %+v", skipped)
	}
}

func TestNotificationToIR_UnsupportedDestinationSkipped(t *testing.T) {
	nc := &tfe.NotificationConfiguration{
		ID:              "nc-3",
		Name:            "teams",
		DestinationType: tfe.NotificationDestinationTypeMicrosoftTeams,
		URL:             "https://teams.example/x",
	}
	_, skipped, ok := notificationToIR(nc, "ws-src", "app")
	if ok {
		t.Error("microsoft-teams destination should be skipped (ok=false)")
	}
	if !hasSkippedKind(skipped, "tfe-notification-unsupported-type") {
		t.Errorf("expected an unsupported-type skipped-item, got %+v", skipped)
	}
	// nil config is skipped cleanly.
	if _, _, ok := notificationToIR(nil, "ws-src", "app"); ok {
		t.Error("nil notification config should be skipped")
	}
}

func TestNotificationToIR_EmailUsersReported(t *testing.T) {
	nc := &tfe.NotificationConfiguration{
		ID:              "nc-4",
		Name:            "email",
		DestinationType: tfe.NotificationDestinationTypeEmail,
		EmailAddresses:  []string{"ops@example.com"},
		EmailUsers:      []*tfe.User{{ID: "user-1"}},
	}
	got, skipped, ok := notificationToIR(nc, "ws-src", "app")
	if !ok {
		t.Fatal("expected ok=true for an email config")
	}
	if len(got.EmailAddresses) != 1 || got.EmailAddresses[0] != "ops@example.com" {
		t.Errorf("email addresses: %+v", got.EmailAddresses)
	}
	if !hasSkippedKind(skipped, "tfe-notification-email-users") {
		t.Errorf("expected an email-users skipped-item, got %+v", skipped)
	}
}

func hasSkippedKind(items []ir.SkippedItem, kind string) bool {
	for _, s := range items {
		if s.Kind == kind {
			return true
		}
	}
	return false
}
