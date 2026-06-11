package publisher

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ProtonMail/go-crypto/openpgp"
	terrapod "github.com/mattrobinsonsre/terrapod/go-terrapod"
)

func TestPublishProviderUploadOrder(t *testing.T) {
	var order []string
	var manifestBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		switch {
		case strings.HasSuffix(r.URL.Path, "/shasums"):
			order = append(order, "shasums")
			manifestBody = string(body)
		case strings.HasSuffix(r.URL.Path, "/shasums.sig"):
			order = append(order, "sig")
		case strings.Contains(r.URL.Path, "/platforms/"):
			order = append(order, "platform:"+r.URL.Path[strings.LastIndex(r.URL.Path, "/platforms/")+len("/platforms/"):])
		}
		w.Header().Set("Content-Type", "application/vnd.api+json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"data":{"id":"v","type":"x","attributes":{}}}`))
	}))
	defer srv.Close()

	c, err := terrapod.NewClient(terrapod.Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}
	entity, err := openpgp.NewEntity("awsmai", "", "security@example.test", nil)
	if err != nil {
		t.Fatal(err)
	}

	err = PublishProvider(context.Background(), c, ProviderInput{
		Name:       "awsmai",
		Version:    "1.0.0",
		SigningKey: entity,
		Binaries:   map[string][]byte{"linux/arm64": []byte("bin")},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}

	// The signature MUST be uploaded before any platform binary (the server's
	// trust gate); the manifest first of all.
	if len(order) != 3 || order[0] != "shasums" || order[1] != "sig" {
		t.Fatalf("upload order = %v, want shasums, sig, platform", order)
	}
	if order[2] != "platform:linux/arm64" {
		t.Errorf("platform path = %s", order[2])
	}
	if !strings.Contains(manifestBody, "terraform-provider-awsmai_1.0.0_linux_arm64.zip") {
		t.Errorf("manifest missing canonical filename: %q", manifestBody)
	}
}

func TestPublishProviderNoBinaries(t *testing.T) {
	c, _ := terrapod.NewClient(terrapod.Options{BaseURL: "http://x", Token: "t"})
	entity, _ := openpgp.NewEntity("x", "", "x@x.test", nil)
	err := PublishProvider(context.Background(), c, ProviderInput{
		Name: "awsmai", Version: "1.0.0", SigningKey: entity, Binaries: nil,
	}, nil)
	if err == nil {
		t.Fatal("expected error for no binaries")
	}
}
