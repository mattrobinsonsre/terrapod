package gpg_key

import (
	"testing"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

// #2310: `source_url` is Optional+Computed, so an explicit `source_url = ""` in
// HCL plans as `""` and the read-back has to hand back that same `""`.
// Collapsing an empty server value to StringNull() makes the apply fail with
// "Provider produced inconsistent result after apply", because a null cannot
// satisfy a planned empty string.
//
// Only the read-back was ever at risk here, unlike the attributes whose SDK
// field needed a pointer. `SourceURL` carries `,omitempty`, so an explicit `""`
// is dropped from the request body -- but the attribute is RequiresReplace, so
// there is no update path and every change is a CREATE. On a create a dropped
// value means the server applies its column default rather than keeping some
// previously stored value, and this column is nullable, so it stores NULL --
// which reads back as `""` all the same (see the parse-path subtest below). So
// the read-back is the only place the empty string could be lost.

func TestAnEmptySourceURLReadsBackAsEmptyNotNull(t *testing.T) {
	for _, tc := range []struct {
		name string
		key  *terrapod.GPGKey
	}{
		// These two are the SAME input at runtime: `SourceURL` is a plain Go
		// string on the SDK struct, so "explicitly empty" and "absent" are both
		// the zero value and the provider cannot tell them apart. Both cases are
		// kept deliberately -- they pin that the read-back stays unconditional
		// rather than growing a guard on a neighbouring field (`source`, say),
		// which is how the collapse would come back. Neither case proves the
		// server distinguishes the two shapes, because at this boundary it
		// cannot; the subtest below is what establishes that convergence.
		{
			name: "the server answers an explicit empty source url",
			key:  &terrapod.GPGKey{ID: "gpg-c0ffee", KeyID: "51A2F3", SourceURL: ""},
		},
		{
			name: "the source url is absent from the server's answer",
			key:  &terrapod.GPGKey{ID: "gpg-c0ffee", KeyID: "51A2F3"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var m gpgKeyModel
			readGPGKeyFromSDK(tc.key, &m)

			if m.SourceURL.IsNull() {
				t.Fatal(`source_url read back as null; a null cannot satisfy an explicit source_url = "" in config`)
			}
			if m.SourceURL.IsUnknown() {
				t.Fatal("source_url read back as unknown; a read must produce a concrete value")
			}
			if got := m.SourceURL.ValueString(); got != "" {
				t.Errorf("source_url = %q, want %q", got, "")
			}
		})
	}

	// The convergence the table above relies on. The column is nullable, so a
	// JSON null -- not an empty string -- is what the server really serialises
	// for an unset source url, and asserting only against `""` would leave the
	// shape that actually occurs untested. Driven through the SDK's own parse
	// path so this is a measurement rather than an assumption: `GetStringAttr`
	// is what fills the struct field, and it returns "" for an absent key, a
	// JSON null and an explicit "" alike.
	t.Run("an absent key, a JSON null and an explicit empty all parse to an empty source url", func(t *testing.T) {
		for _, body := range []string{
			`{"data":{"id":"gpg-c0ffee","type":"gpg-keys","attributes":{"key-id":"51A2F3"}}}`,
			`{"data":{"id":"gpg-c0ffee","type":"gpg-keys","attributes":{"key-id":"51A2F3","source-url":null}}}`,
			`{"data":{"id":"gpg-c0ffee","type":"gpg-keys","attributes":{"key-id":"51A2F3","source-url":""}}}`,
		} {
			res, err := terrapod.ParseResource([]byte(body))
			if err != nil {
				t.Fatalf("ParseResource(%s): %v", body, err)
			}
			if got := terrapod.GetStringAttr(res, "source-url"); got != "" {
				t.Errorf("GetStringAttr(source-url) = %q, want \"\"; the table above assumes "+
					"every empty shape reaches the provider as the zero value", got)
			}

			// ...and that zero value must still come out of the read-back
			// concrete, which is the property the nullable column makes worth
			// asserting end to end rather than only at the SDK boundary.
			var m gpgKeyModel
			readGPGKeyFromSDK(&terrapod.GPGKey{
				ID:        res.ID,
				KeyID:     terrapod.GetStringAttr(res, "key-id"),
				SourceURL: terrapod.GetStringAttr(res, "source-url"),
			}, &m)
			if m.SourceURL.IsNull() {
				t.Errorf("source_url read back as null for wire shape %s", body)
			}
		}
	})
}

func TestANonEmptySourceURLStillReadsBackVerbatim(t *testing.T) {
	// The empty case is the only thing that changed, so a real source url must
	// still arrive intact -- the fix must not be satisfiable by flattening every
	// value to "".
	const want = "https://example.com/keys/51A2F3.asc"

	var m gpgKeyModel
	readGPGKeyFromSDK(&terrapod.GPGKey{
		ID:        "gpg-c0ffee",
		KeyID:     "51A2F3",
		SourceURL: want,
	}, &m)

	if got := m.SourceURL.ValueString(); got != want {
		t.Errorf("source_url = %q, want %q", got, want)
	}
}
