package notification_configuration

import (
	"context"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/types"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// `url` is Optional+Computed, so an explicit "" in config plans as "" and the
// apply must come back with "" -- a null read-back raises "Provider produced
// inconsistent result after apply" (#2310).
//
// An empty url is legitimate for exactly one destination type, which is what
// makes this attribute worth a test rather than a refusal. The server reads
// `url = attrs.get("url", "")` and then refuses only for the webhook types:
//
//	generic, slack : 422 "url is required for generic and slack types"
//	email          : accepted -- a webhook URL is meaningless for email, where
//	                 delivery is addressed by `email_addresses` instead
//
// So the round trip that has to hold is the email one, and "" is the ordinary
// value there rather than a degenerate input.
func TestURLRoundTripsAnExplicitEmptyStringForAnEmailDestination(t *testing.T) {
	m := notificationConfigModel{
		Name:            types.StringValue("x"),
		DestinationType: types.StringValue("email"),
		URL:             types.StringValue(""),
	}

	nc := &terrapod.NotificationConfiguration{
		ID: "nc-c0ffee", Name: "x", DestinationType: "email", URL: "",
	}
	if d := readNCFromSDK(context.Background(), nc, &m); d.HasError() {
		t.Fatalf("readNCFromSDK: %v", d)
	}
	if m.URL != types.StringValue("") {
		t.Errorf("the round trip changed the value: planned %v, got %v", types.StringValue(""), m.URL)
	}
}

// Both wire shapes an empty url can arrive as, driven through the read-back.
//
// They converge before the provider sees them -- and that convergence is a
// measurement, not an assumption, so the subtest below proves it through the
// SDK's own parse path rather than taking it on trust. `URL` is a plain Go
// string on the SDK struct, filled by `GetStringAttr`, which returns "" for an
// absent key, a JSON null and an explicit "" alike. So the provider is handed
// the zero value for all three and cannot tell them apart; what it must not do
// is turn that zero value back into null.
func TestURLReadsBackAsAnEmptyStringRatherThanNull(t *testing.T) {
	for _, tc := range []struct {
		name string
		nc   *terrapod.NotificationConfiguration
	}{
		{"an empty url, as an email destination legitimately carries", &terrapod.NotificationConfiguration{
			ID: "nc-c0ffee", Name: "x", DestinationType: "email", URL: "",
		}},
		{"an absent url, which reaches the SDK struct as the zero value", &terrapod.NotificationConfiguration{
			ID: "nc-c0ffee", Name: "x", DestinationType: "email",
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m notificationConfigModel
			if d := readNCFromSDK(context.Background(), tc.nc, &m); d.HasError() {
				t.Fatalf("readNCFromSDK: %v", d)
			}
			if m.URL.IsNull() {
				t.Errorf("url read back as null; it is Optional+Computed, so a null " +
					"result cannot satisfy an explicit \"\" in config")
			}
			if m.URL.ValueString() != "" {
				t.Errorf("url read back as %q, want \"\"", m.URL.ValueString())
			}
		})
	}

	// The convergence the table above relies on: an absent key and a JSON null
	// both reach the SDK struct as "". Asserted against the real parse path, so
	// the two cases above are known to be the shapes the server can produce
	// rather than two spellings of the same Go literal.
	t.Run("an absent key and a JSON null both parse to an empty url", func(t *testing.T) {
		for _, body := range []string{
			`{"data":{"id":"nc-c0ffee","type":"notification-configurations",` +
				`"attributes":{"name":"x","destination-type":"email"}}}`,
			`{"data":{"id":"nc-c0ffee","type":"notification-configurations",` +
				`"attributes":{"name":"x","destination-type":"email","url":null}}}`,
		} {
			res, err := terrapod.ParseResource([]byte(body))
			if err != nil {
				t.Fatalf("ParseResource: %v", err)
			}
			if got := terrapod.GetStringAttr(res, "url"); got != "" {
				t.Errorf("GetStringAttr(url) = %q, want \"\"; the table above assumes "+
					"every empty shape reaches the provider as the zero value", got)
			}
		}
	})
}
