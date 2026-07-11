package terrapod

// Exported-surface contract gate (#550).
//
// go-terrapod is the public canonical Go SDK: terraform-provider-terrapod,
// terrapod-migrate, terrapod-publish, and third-party automation all import it.
// Removing or renaming an exported function/method/type, or changing an exported
// struct field's name/type/json tag, is a BREAKING change for every one of those
// consumers — the Go analogue of dropping an API route or response attribute.
//
// This test parses the package with go/ast and freezes the exported surface
// (exported funcs, methods, types, and struct fields with their rendered type +
// json tag) against a committed golden file (surface.golden). A removal/rename
// fails the build as breaking (MAJOR bump or a documented deprecation window, not
// a regen); an addition is additive — regenerate the golden with:
//
//	UPDATE_SURFACE=1 go test ./... -run TestExportedSurface
//
// It reads the package source directly (no build/reflection), so it is fast and
// has no external dependency.

import (
	"bytes"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"sort"
	"strings"
	"testing"
)

const surfaceGolden = "surface.golden"

func renderType(fset *token.FileSet, n ast.Node) string {
	var b bytes.Buffer
	_ = printer.Fprint(&b, fset, n)
	// Collapse all internal whitespace/newlines to single spaces for a stable,
	// diff-friendly one-line rendering.
	return strings.Join(strings.Fields(b.String()), " ")
}

// funcSignature renders an *ast.FuncType as "(params) results" (the "func"
// keyword stripped), so it can be prefixed with a name/receiver.
func funcSignature(fset *token.FileSet, ft *ast.FuncType) string {
	s := renderType(fset, ft)
	return strings.TrimPrefix(s, "func")
}

func exportedSurface(t *testing.T) []string {
	t.Helper()
	fset := token.NewFileSet()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}

	var out []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		for _, decl := range file.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if !d.Name.IsExported() {
					continue
				}
				sig := funcSignature(fset, d.Type)
				if d.Recv != nil && len(d.Recv.List) > 0 {
					recv := renderType(fset, d.Recv.List[0].Type)
					// Skip methods on unexported receivers.
					base := strings.TrimPrefix(recv, "*")
					if base == "" || !ast.IsExported(base) {
						continue
					}
					out = append(out, "method ("+recv+") "+d.Name.Name+sig)
				} else {
					out = append(out, "func "+d.Name.Name+sig)
				}
			case *ast.GenDecl:
				out = append(out, genDeclSurface(fset, d)...)
			}
		}
	}
	sort.Strings(out)
	return out
}

func genDeclSurface(fset *token.FileSet, d *ast.GenDecl) []string {
	var out []string
	for _, spec := range d.Specs {
		switch s := spec.(type) {
		case *ast.TypeSpec:
			if !s.Name.IsExported() {
				continue
			}
			if st, ok := s.Type.(*ast.StructType); ok {
				out = append(out, "type "+s.Name.Name+" struct")
				out = append(out, structFields(fset, s.Name.Name, st)...)
			} else {
				out = append(out, "type "+s.Name.Name+" "+renderType(fset, s.Type))
			}
		case *ast.ValueSpec:
			for _, name := range s.Names {
				if name.IsExported() {
					kind := "var"
					if d.Tok == token.CONST {
						kind = "const"
					}
					out = append(out, kind+" "+name.Name)
				}
			}
		}
	}
	return out
}

func structFields(fset *token.FileSet, typeName string, st *ast.StructType) []string {
	var out []string
	for _, f := range st.Fields.List {
		tag := ""
		if f.Tag != nil {
			tag = " " + f.Tag.Value
		}
		ftype := renderType(fset, f.Type)
		if len(f.Names) == 0 {
			// Embedded field — the type name is the field name.
			out = append(out, "field "+typeName+"."+ftype+tag)
			continue
		}
		for _, name := range f.Names {
			if name.IsExported() {
				out = append(out, "field "+typeName+"."+name.Name+" "+ftype+tag)
			}
		}
	}
	return out
}

func TestExportedSurface(t *testing.T) {
	current := exportedSurface(t)

	if os.Getenv("UPDATE_SURFACE") != "" {
		if err := os.WriteFile(surfaceGolden, []byte(strings.Join(current, "\n")+"\n"), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		return
	}

	raw, err := os.ReadFile(surfaceGolden)
	if err != nil {
		t.Fatalf("read %s (generate with UPDATE_SURFACE=1 go test ./... -run TestExportedSurface): %v", surfaceGolden, err)
	}
	golden := map[string]bool{}
	for line := range strings.SplitSeq(strings.TrimSpace(string(raw)), "\n") {
		if line != "" {
			golden[line] = true
		}
	}
	cur := map[string]bool{}
	for _, line := range current {
		cur[line] = true
	}

	var removed, added []string
	for line := range golden {
		if !cur[line] {
			removed = append(removed, line)
		}
	}
	for line := range cur {
		if !golden[line] {
			added = append(added, line)
		}
	}
	sort.Strings(removed)
	sort.Strings(added)

	if len(removed) > 0 {
		t.Errorf("BREAKING: exported go-terrapod surface removed/renamed (consumers import these — MAJOR bump or a documented deprecation window, NOT a golden regen):\n  %s", strings.Join(removed, "\n  "))
	}
	if len(added) > 0 {
		t.Errorf("Exported surface added (additive). Regenerate the golden:\n  UPDATE_SURFACE=1 go test ./... -run TestExportedSurface\n  added:\n  %s", strings.Join(added, "\n  "))
	}
}
