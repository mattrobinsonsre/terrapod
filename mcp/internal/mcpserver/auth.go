package mcpserver

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// ResolveToken returns the API token in precedence order — the explicit flag,
// then $TERRAPOD_TOKEN, then the terraform/tofu CLI credentials file for the
// host (what `tofu login <host>` writes). This mirrors terrapod-publish so an
// agent user reuses the login they already have; no separate PAT.
func ResolveToken(flagToken, host string) (string, error) {
	if flagToken != "" {
		return flagToken, nil
	}
	if env := os.Getenv("TERRAPOD_TOKEN"); env != "" {
		return env, nil
	}
	if tok := tokenFromCredentialsFile(host); tok != "" {
		return tok, nil
	}
	return "", fmt.Errorf(
		"no API token for %q: pass --token, set TERRAPOD_TOKEN, or run `tofu login %s`", host, host)
}

// tokenFromCredentialsFile reads ~/.terraform.d/credentials.tfrc.json and
// returns the token stored for host. The file is host-keyed, so a per-instance
// server reads only its own instance's token.
func tokenFromCredentialsFile(host string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	data, err := os.ReadFile(filepath.Join(home, ".terraform.d", "credentials.tfrc.json"))
	if err != nil {
		return ""
	}
	var doc struct {
		Credentials map[string]struct {
			Token string `json:"token"`
		} `json:"credentials"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return ""
	}
	return doc.Credentials[host].Token
}

// ListCredentialHosts returns the hosts present in the credentials file — used
// by the multi-host `list_instances` tool and for friendlier errors.
func ListCredentialHosts() []string {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(filepath.Join(home, ".terraform.d", "credentials.tfrc.json"))
	if err != nil {
		return nil
	}
	var doc struct {
		Credentials map[string]json.RawMessage `json:"credentials"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil
	}
	hosts := make([]string, 0, len(doc.Credentials))
	for h := range doc.Credentials {
		hosts = append(hosts, h)
	}
	return hosts
}
