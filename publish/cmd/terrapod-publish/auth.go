package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// resolveToken returns the API token in precedence order: the explicit flag,
// then $TERRAPOD_TOKEN, then the terraform CLI credentials file for the host.
func resolveToken(flagToken, host string) (string, error) {
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
		"no API token found: pass --token, set TERRAPOD_TOKEN, or run `terraform login %s`", host)
}

// tokenFromCredentialsFile reads ~/.terraform.d/credentials.tfrc.json and
// returns the token stored for host (the file `terraform login` writes).
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
