package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"

	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
	"github.com/mattrobinsonsre/terrapod/publish/internal/publisher"
	"github.com/mattrobinsonsre/terrapod/publish/internal/sign"
)

func providerCmd(args []string) int {
	fs := flag.NewFlagSet("provider", flag.ContinueOnError)
	host := fs.String("host", "", "Terrapod hostname (required)")
	name := fs.String("name", "", "provider name, e.g. awsmai (required)")
	version := fs.String("version", "", "version, e.g. 1.0.0 (no leading v) (required)")
	keyPath := fs.String("signing-key", "", "path to ASCII-armored private signing key (required)")
	keyPass := fs.String("signing-key-passphrase", "",
		"passphrase for the signing key (or $TERRAPOD_SIGNING_KEY_PASSPHRASE)")
	token := fs.String("token", "", "API token (else $TERRAPOD_TOKEN or credentials.tfrc.json)")
	var binaries multiFlag
	fs.Var(&binaries, "binary", "platform binary as OS/ARCH=PATH (repeatable, required)")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	if m := firstMissing([]struct{ name, val string }{
		{"host", *host}, {"name", *name}, {"version", *version}, {"signing-key", *keyPath},
	}); m != "" {
		fmt.Fprintf(os.Stderr, "missing required flag: --%s\n", m)
		return 2
	}
	if len(binaries) == 0 {
		fmt.Fprintln(os.Stderr, "missing required flag: --binary (at least one)")
		return 2
	}

	armored, err := os.ReadFile(*keyPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read signing key: %v\n", err)
		return 1
	}
	pass := *keyPass
	if pass == "" {
		pass = os.Getenv("TERRAPOD_SIGNING_KEY_PASSPHRASE")
	}
	entity, err := sign.LoadPrivateKey(string(armored), pass)
	if err != nil {
		fmt.Fprintf(os.Stderr, "load signing key: %v\n", err)
		return 1
	}

	bins := make(map[string][]byte, len(binaries))
	for _, spec := range binaries {
		platform, path, ok := strings.Cut(spec, "=")
		if !ok || platform == "" || path == "" {
			fmt.Fprintf(os.Stderr, "invalid --binary %q, want OS/ARCH=PATH\n", spec)
			return 2
		}
		data, err := os.ReadFile(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "read binary %s: %v\n", path, err)
			return 1
		}
		bins[platform] = data
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

	err = publisher.PublishProvider(context.Background(), client, publisher.ProviderInput{
		Name:       *name,
		Version:    *version,
		SigningKey: entity,
		Binaries:   bins,
	}, func(s string) { fmt.Fprintf(os.Stderr, "==> %s\n", s) })
	if err != nil {
		fmt.Fprintf(os.Stderr, "publish failed: %v\n", err)
		return 1
	}
	return 0
}
