// Command terrapod-publish publishes providers and modules to a Terrapod
// private registry. Provider artifacts are zipped and the SHA256SUMS manifest
// is GPG-signed entirely in-process (pure Go, no gpg/zip/tar binaries).
package main

import (
	"fmt"
	"os"
	"strings"
)

// Version is stamped at build time via -ldflags "-X main.Version=...".
var Version = "dev"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	rest := os.Args[2:]
	switch os.Args[1] {
	case "provider":
		os.Exit(providerCmd(rest))
	case "module":
		os.Exit(moduleCmd(rest))
	case "version", "-v", "--version":
		fmt.Println(Version)
	case "help", "-h", "--help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `terrapod-publish — publish providers and modules to a Terrapod private registry

Usage:
  terrapod-publish provider --host H --name N --version V --signing-key KEY \
                            --binary OS/ARCH=PATH [--binary OS/ARCH=PATH ...]
  terrapod-publish module   --host H --name N --provider P --version V --source DIR

Auth resolution order: --token, then $TERRAPOD_TOKEN, then
~/.terraform.d/credentials.tfrc.json for the target host (i.e. `+"`terraform login`"+`).
`)
}

// multiFlag is a repeatable string flag.
type multiFlag []string

func (m *multiFlag) String() string  { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}

// firstMissing returns the name of the first empty required flag, or "".
func firstMissing(required []struct{ name, val string }) string {
	for _, r := range required {
		if r.val == "" {
			return r.name
		}
	}
	return ""
}
