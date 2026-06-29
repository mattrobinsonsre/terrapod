package terrapod

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWarmCacheBulk(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/admin/binary-cache/warm-bulk") {
			http.Error(w, "unhandled", http.StatusNotFound)
			return
		}
		b, _ := io.ReadAll(r.Body)
		body := string(b)
		// The request must forward both the binary and the provider entry.
		if !strings.Contains(body, "\"tofu\"") || !strings.Contains(body, "hashicorp/aws") {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		// One success, one failure — exercises partial-outcome reporting.
		_, _ = w.Write([]byte(`{"total":2,"succeeded":1,"failed":1,"results":[
		  {"kind":"binary","ref":"tofu 1.9.0 linux/amd64","ok":true},
		  {"kind":"provider","ref":"registry.terraform.io/hashicorp/aws 5.60.0 linux/amd64","ok":false,"error":"upstream 404"}
		]}`))
	}))
	t.Cleanup(srv.Close)

	c, err := NewClient(Options{BaseURL: srv.URL, Token: "t"})
	if err != nil {
		t.Fatal(err)
	}

	resp, err := c.WarmCacheBulk(context.Background(), BulkWarmRequest{
		Binaries: []WarmBinaryEntry{
			{Tool: "tofu", Version: "1.9.0", Platforms: []WarmPlatform{{OS: "linux", Arch: "amd64"}}},
		},
		Providers: []WarmProviderEntry{
			{Source: "registry.terraform.io/hashicorp/aws", Version: "5.60.0"},
		},
	})
	if err != nil {
		t.Fatalf("WarmCacheBulk: %v", err)
	}
	if resp.Total != 2 || resp.Succeeded != 1 || resp.Failed != 1 {
		t.Fatalf("unexpected totals: %+v", resp)
	}
	if len(resp.Results) != 2 || resp.Results[1].OK || resp.Results[1].Error != "upstream 404" {
		t.Fatalf("unexpected results: %+v", resp.Results)
	}
}
