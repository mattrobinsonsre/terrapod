// Command pulumi-tfgen-terrapod generates the Pulumi Package Schema and the
// per-language SDKs from the bridged Terraform provider.
//
// The schema is a committed artifact (`schema.json`) and the SDKs are not: the
// schema IS the contract, so a diff on it is reviewable in the way this
// repository's other golden files are, while four languages' generated SDKs
// would be a very large block of generated code in every clone for no review
// value. The release job regenerates the SDKs from the committed schema.
package main

import (
	pftfgen "github.com/pulumi/pulumi-terraform-bridge/v3/pkg/pf/tfgen"

	terrapod "github.com/mattrobinsonsre/terrapod/pulumi-provider"
)

// Set via ldflags at build time.
//
// Semver, unlike the Terraform provider's `dev`: the bridge validates this and
// refuses to generate anything for a version it cannot parse, so a non-semver
// default would fail at the first invocation rather than at release time.
var version = terrapod.SourceBuildVersion

func main() {
	pftfgen.Main(terrapod.Namespace, terrapod.Provider(version))
}
