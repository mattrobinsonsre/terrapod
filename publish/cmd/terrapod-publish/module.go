package main

import (
	"context"
	"flag"
	"fmt"
	"os"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/publish/internal/publisher"
)

func moduleCmd(args []string) int {
	fs := flag.NewFlagSet("module", flag.ContinueOnError)
	host := fs.String("host", "", "Terrapod hostname (required)")
	name := fs.String("name", "", "module name (required)")
	provider := fs.String("provider", "", "module provider, e.g. aws (required)")
	version := fs.String("version", "", "version, e.g. 1.2.3 (no leading v) (required)")
	source := fs.String("source", "", "path to the module source directory (required)")
	token := fs.String("token", "", "API token (else $TERRAPOD_TOKEN or credentials.tfrc.json)")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	if m := firstMissing([]struct{ name, val string }{
		{"host", *host}, {"name", *name}, {"provider", *provider},
		{"version", *version}, {"source", *source},
	}); m != "" {
		fmt.Fprintf(os.Stderr, "missing required flag: --%s\n", m)
		return 2
	}

	tok, err := resolveToken(*token, *host)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	client, err := terrapod.NewClient(terrapod.Options{BaseURL: *host, Token: tok})
	if err != nil {
		fmt.Fprintf(os.Stderr, "client: %v\n", err)
		return 1
	}

	err = publisher.PublishModule(
		context.Background(), client, *name, *provider, *version, *source,
		func(s string) { fmt.Fprintf(os.Stderr, "==> %s\n", s) },
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "publish failed: %v\n", err)
		return 1
	}
	return 0
}
