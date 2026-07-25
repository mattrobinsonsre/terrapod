package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"io"
	"testing"
	"time"
)

func TestPercentiles(t *testing.T) {
	ds := make([]time.Duration, 100)
	for i := range ds {
		ds[i] = time.Duration(i+1) * time.Millisecond // 1..100ms, shuffled order irrelevant (sorted inside)
	}
	// reverse to prove it sorts
	for i, j := 0, len(ds)-1; i < j; i, j = i+1, j-1 {
		ds[i], ds[j] = ds[j], ds[i]
	}
	p50, p95, p99, max := percentiles(ds)
	if p50 != 50*time.Millisecond {
		t.Errorf("p50 = %v, want 50ms", p50)
	}
	if p95 != 95*time.Millisecond {
		t.Errorf("p95 = %v, want 95ms", p95)
	}
	if p99 != 99*time.Millisecond {
		t.Errorf("p99 = %v, want 99ms", p99)
	}
	if max != 100*time.Millisecond {
		t.Errorf("max = %v, want 100ms", max)
	}
}

func TestPercentilesEmpty(t *testing.T) {
	p50, _, _, max := percentiles(nil)
	if p50 != 0 || max != 0 {
		t.Errorf("empty percentiles should be zero, got p50=%v max=%v", p50, max)
	}
}

func TestHostOf(t *testing.T) {
	cases := map[string]string{
		"https://terrapod.local":       "terrapod.local",
		"http://localhost:18000":       "localhost:18000",
		"https://terrapod.example.com": "terrapod.example.com",
	}
	for in, want := range cases {
		got, err := hostOf(in)
		if err != nil {
			t.Fatalf("hostOf(%q): %v", in, err)
		}
		if got != want {
			t.Errorf("hostOf(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTrivialConfigTarGz(t *testing.T) {
	blob, err := trivialConfigTarGz()
	if err != nil {
		t.Fatal(err)
	}
	gz, err := gzip.NewReader(bytes.NewReader(blob))
	if err != nil {
		t.Fatalf("not gzip: %v", err)
	}
	tr := tar.NewReader(gz)
	hdr, err := tr.Next()
	if err != nil {
		t.Fatalf("no tar entry: %v", err)
	}
	if hdr.Name != "main.tf" {
		t.Errorf("entry name = %q, want main.tf", hdr.Name)
	}
	body, _ := io.ReadAll(tr)
	if !bytes.Contains(body, []byte("output")) {
		t.Errorf("main.tf missing an output block: %q", body)
	}
}
