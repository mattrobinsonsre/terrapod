// Package terrapod is the Pulumi provider for Terrapod, bridged from the
// Terraform provider rather than written by hand.
//
// **Why bridged** (decided 2026-10-07, #1987): `terraform-provider-terrapod` is
// a thin wrapper over `go-terrapod`, which is the canonical Go view of the API.
// Deriving the Pulumi provider from it means a resource added there appears
// here with no second surface to keep in step -- the API-consumer contract
// working rather than being worked around. The accepted cost is that the
// ergonomics are Terraform-shaped: naming and types follow the Terraform
// schema rather than idiomatic Pulumi. The stated condition for revisiting
// that is Pulumi adoption growing enough to justify owning a second provider.
//
// **Why its own module** rather than a package inside `provider/`: the bridge
// pulls in a large dependency tree, and the Terraform provider is deliberately
// thin. Keeping them separate means a `terraform` user's build does not carry
// Pulumi's.
package terrapod

import (
	_ "embed"

	pftfbridge "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/pf/tfbridge"
	"github.com/pulumi/pulumi-terraform-bridge/v3/pkg/tfbridge"
	"github.com/pulumi/pulumi-terraform-bridge/v3/pkg/tfbridge/tokens"

	"github.com/mattrobinsonsre/terrapod/provider/tfprovider"
)

// Namespace is the Pulumi package name. It is `terrapod`, matching the
// Terraform provider, so a practitioner reading either set of docs sees one
// product rather than two.
const Namespace = "terrapod"

// bridgeMetadata is the bridge's own persisted mapping state, embedded so the
// provider binary is self-contained.
//
// It starts as `{}` and the generator rewrites it, in place, beside the schema
// -- which is why it is embedded from the cmd directory rather than from here:
// every generated artifact lives in one place, so nothing can be regenerated
// into a directory the embed does not read. It is committed for the same reason
// `schema.json` is: it is an input the bridge reuses between runs, so a change
// to it is a change to how the surface maps, and that belongs in a diff.
//
//go:embed cmd/pulumi-resource-terrapod/bridge-metadata.json
var bridgeMetadata []byte

// : The repository's licence, as the bridge spells it. Taken by address because
// : `TFProviderLicense` is a pointer field -- nil means "use the default".
var mpl20 = tfbridge.MPL20LicenseType

// mainModule is the single Pulumi module every resource lands in.
//
// Terraform has a flat `terrapod_*` namespace with no sub-grouping, so there is
// nothing to infer modules FROM -- `tokens.InferredModules` would split on
// shared name prefixes and invent a taxonomy the Terraform provider does not
// have, which would then drift as resources were added. One module keeps the
// mapping total and mechanical.
const mainModule = "index"

// : The version both commands default to for a source build.
//
// : It is SEMVER rather than the Terraform provider's bare `dev`, because the
// : bridge VALIDATES this value and refuses to serve or generate anything for a
// : version it cannot parse. That forces the split in `sdkVersion` below.
const SourceBuildVersion = "0.0.0-dev"

// sdkVersion translates the bridge's version into the one go-terrapod wants.
//
// Two consumers, two requirements, and they conflict for a source build:
//
//   - the BRIDGE needs semver and rejects anything else, so a source build is
//     `0.0.0-dev`;
//   - `go-terrapod`'s `VersionCheck` skips the SDK-vs-API compatibility probe
//     ONLY for the literal `dev` or an empty string. Hand it `0.0.0-dev` and it
//     performs a real comparison against the API's version and reports a
//     mismatch -- so a locally built plugin would fail with a version error that
//     has nothing to do with Pulumi, which is precisely the local-development
//     case the Terraform provider's own `New` docstring protects by passing
//     `dev`.
//
// So a source build tells the SDK `dev` and a release build tells it the real
// version, which is what makes the compatibility check fire for operators and
// stay out of the way during development.
func sdkVersion(version string) string {
	if version == SourceBuildVersion || version == "" {
		return "dev"
	}
	return version
}

// Provider returns the bridged provider definition.
//
// `version` is the Pulumi package version. It reaches the Terraform provider as
// `sdkVersion(version)`, which assigns `terrapod.SDKVersion` -- go-terrapod
// sends that as the tool argument to its API version check, so a Pulumi run
// identifies itself as the Pulumi provider rather than impersonating the
// Terraform one.
func Provider(version string) tfbridge.ProviderInfo {
	info := tfbridge.ProviderInfo{
		P:    pftfbridge.ShimProvider(tfprovider.New(sdkVersion(version))()),
		Name: Namespace,

		DisplayName: "Terrapod",
		Publisher:   "mattrobinsonsre",
		Version:     version,

		// MPL-2.0, matching the repository. The bridge's own default is already
		// `MPL 2.0`, so this is redundant TODAY and set anyway: the bridge
		// renders it into every generated SDK's package metadata, so inheriting
		// a default that later changed would misstate the licence in four
		// package registries at once.
		TFProviderLicense: &mpl20,

		// SPDX, and a DIFFERENT field from `TFProviderLicence` below: this is
		// "the licence the resulting package has", and its default is NONE. Left
		// unset, all four published SDKs would declare no licence at all for an
		// MPL-2.0 project -- which the generated schema confirms, since
		// `license` is absent from it until this is set.
		License: "MPL-2.0",

		Description: "A Pulumi package for managing Terrapod resources.",
		Keywords:    []string{"pulumi", "terrapod", "terraform", "opentofu", "ansible", "category/infrastructure"},
		Homepage:    "https://github.com/mattrobinsonsre/terrapod",
		Repository:  "https://github.com/mattrobinsonsre/terrapod",
		GitHubOrg:   "mattrobinsonsre",

		// Where the wrapped provider's SOURCE is, for the docs pass.
		//
		// Left blank the bridge infers "where Go downloaded the build", which
		// for a local `replace` resolves to `pulumi-provider/provider` -- a
		// path that does not exist, so every generation printed
		// `error running 'go mod download -json'` and carried on. Generation
		// still succeeded, which is the problem: an error on every run is how
		// people learn to stop reading the output.
		UpstreamRepoPath: "../provider",

		// The Terraform provider's own config attributes carry their
		// `TERRAPOD_*` environment fallbacks in its schema, and the bridge
		// preserves them -- which is what makes an empty provider block work
		// inside an agent run, where `provider_credentials` exports the pair.
		Config: map[string]*tfbridge.SchemaInfo{},

		MetadataInfo: tfbridge.NewProviderMetadata(bridgeMetadata),

		// All four languages, and the package NAMES are set explicitly rather
		// than defaulted because Terrapod serves these packages itself (decided
		// 2026-10-28) -- the name is what an operator types against their own
		// instance's package cache, so it is an operator-facing contract rather
		// than an implementation detail.
		//
		// Go and .NET need their blocks for a second reason: without them the
		// generator emits a `language` section containing only `nodejs` and
		// `python`, so two of the four SDKs cannot be generated at all. That is
		// not an error -- it is a silently smaller surface, which is why it is
		// asserted in the schema test rather than trusted.
		JavaScript: &tfbridge.JavaScriptInfo{
			PackageName: "@terrapod/pulumi",
		},
		Python: &tfbridge.PythonInfo{
			// UNDERSCORED, and that is load-bearing rather than cosmetic.
			// `PackageName` is one field with two uses: the generator makes it
			// both the distribution name AND the module DIRECTORY. A hyphen is
			// legal in the first and illegal in the second, so
			// `terrapod-pulumi` produced an SDK that installs and can never be
			// imported -- `import terrapod_pulumi` is ModuleNotFoundError and
			// `import terrapod-pulumi` is a SyntaxError. Measured, by installing
			// it into a venv and trying both (#2267).
			//
			// Underscored it generates `terrapod_pulumi/`, which imports, and
			// pip still resolves `pip install terrapod-pulumi` because PEP 503
			// normalises the two to the same name. So this costs nothing and is
			// the convention every published Pulumi Python SDK follows
			// (`pulumi_aws`, `pulumi_gcp`).
			PackageName: "terrapod_pulumi",
		},
		Golang: &tfbridge.GolangInfo{
			ImportBasePath: "github.com/mattrobinsonsre/terrapod/pulumi-provider/sdk/go/terrapod",
			// Containers (slices, maps, pointers) for every resource. Pulumi's
			// Go programs need them to build collections of resources, which is
			// exactly the shape an inventory declaration takes -- one host per
			// element of whatever the program just provisioned.
			GenerateResourceContainerTypes: true,
		},
		CSharp: &tfbridge.CSharpInfo{
			RootNamespace: "Terrapod",
		},
	}

	// DERIVED, not a hand-written table. 35 resources and 14 data sources map
	// mechanically from `terrapod_x` to `terrapod:index:X`, so a resource added
	// to the Terraform provider appears here with no edit. A literal map would
	// be 49 entries that silently go stale -- the ledger-with-no-gate shape.
	info.MustComputeTokens(tokens.SingleModule(
		Namespace+"_", mainModule, tokens.MakeStandard(Namespace),
	))

	// The ONE name collision in the generated SDKs, and it is structural rather
	// than a mistake anyone made.
	//
	// `terrapod_catalog_item_interface` has an attribute literally called
	// `outputs` -- a module's outputs, which is the right name for it in
	// Terraform. Pulumi derives a nested type's token from the function token
	// plus the property name, giving `getCatalogItemInterfaceOutput`; and it
	// ALSO emits an output-form invoke helper named `<Function>Output` for
	// every function, which for this one is `GetCatalogItemInterfaceOutput`.
	// Same name. In Go that is a redeclaration, so the generated SDK does not
	// compile at all -- eleven errors from this single name, because it
	// cascades into `...OutputArgs` and its `ElementType` too.
	//
	// Renaming the PROPERTY is the only lever that works, and it fixes the type
	// name as a consequence: the token becomes
	// `getCatalogItemInterfaceModuleOutput`, which collides with nothing.
	// Measured against bridge v3.142.0 and Pulumi 3.272.0 -- the generated Go
	// SDK builds clean afterwards, and the schema carries zero collisions
	// derived from its own function list.
	//
	// Two alternatives were tried and do NOT work, so do not reach for them:
	//
	//   - `SchemaInfo.NestedType`, whose doc says "a type to override when the
	//     property is a nested structure", is silently ignored here -- both on
	//     the field and on `Elem`. It reads like the precise lever for this and
	//     is not.
	//   - Renaming the data source TOKEN cannot fix it. The colliding type name
	//     is derived from the token AND the property, so any `getX` with an
	//     `outputs` property collides with `getX`'s own helper whatever X is.
	//     It would change a user-facing name for nothing.
	//
	// The cost is that this one property is `moduleOutputs` in Pulumi and
	// `outputs` in Terraform. That divergence is a bridged provider's normal
	// recourse, it is arguably the clearer name, and it is stated in
	// `docs/pulumi-provider.md` so a practitioner reading one surface is not
	// surprised by the other.
	if ds, ok := info.DataSources["terrapod_catalog_item_interface"]; ok && ds != nil {
		if ds.Fields == nil {
			ds.Fields = map[string]*tfbridge.SchemaInfo{}
		}
		ds.Fields["outputs"] = &tfbridge.SchemaInfo{Name: "moduleOutputs"}
	}

	info.SetAutonaming(255, "-")

	return info
}
