package terrapod

import (
	"encoding/json"
	"fmt"
)

// schemaVersionKey is the Pulumi Package Schema's own version field.
const schemaVersionKey = "version"

// SchemaWithVersion returns the generated Package Schema with its `version` set.
//
// **It has to be done here, and that is the bridge's design rather than a
// workaround.** `pulumi-tfgen` deliberately clears the version when it emits
// `schema.json`, with its own comment saying why: *"Omit the version so that the
// spec is stable if the version is e.g. derived from the current Git commit
// hash."* So the committed artifact is version-less ON PURPOSE, and stays
// reviewable as a diff that does not churn every release.
//
// The plugin-framework provider server then serves `ProviderMetadata.PackageSchema`
// VERBATIM -- it parses `info.Version` for its own use and never injects it into
// the schema it returns, unlike the SDKv2 path, whose `tfbridge.NewProvider`
// takes the version as an explicit argument. The result is that nothing supplies
// it, and a schema with no version cannot generate an SDK at all:
//
//	error: #/version: version must be provided when package supports packing
//
// Which broke the provider's ONLY documented consumption route -- `pulumi
// package add`, the local-SDK path that exists instead of publishing to npm,
// PyPI and NuGet (#2267). It failed for every language and every form of the
// command, before any language-specific work, so one probe established it for
// all four.
//
// **A map round-trip rather than `pschema.PackageSpec`.** Unmarshalling into the
// typed spec and re-marshalling would silently drop any key the bridge emits
// that the struct of the day does not model, which is a whole-surface risk taken
// to set one field. A map preserves every byte of meaning and touches only the
// key named above.
func SchemaWithVersion(schema []byte, version string) ([]byte, error) {
	if version == "" {
		return nil, fmt.Errorf("refusing to serve a schema with an empty version")
	}
	var spec map[string]any
	if err := json.Unmarshal(schema, &spec); err != nil {
		return nil, fmt.Errorf("the embedded package schema is not valid JSON: %w", err)
	}
	spec[schemaVersionKey] = version
	out, err := json.Marshal(spec)
	if err != nil {
		return nil, fmt.Errorf("re-marshalling the package schema: %w", err)
	}
	return out, nil
}
