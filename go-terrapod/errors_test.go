package terrapod

import (
	"errors"
	"fmt"
	"testing"
)

func TestNotFoundError_FormatVariants(t *testing.T) {
	cases := []struct {
		err  NotFoundError
		want string
	}{
		{NotFoundError{Resource: "workspace", ID: "ws-abc"}, `workspace "ws-abc" not found`},
		{NotFoundError{Resource: "workspace"}, "workspace not found"},
		{NotFoundError{}, "resource not found"},
	}
	for _, c := range cases {
		if got := c.err.Error(); got != c.want {
			t.Errorf("NotFoundError(%+v).Error() = %q, want %q", c.err, got, c.want)
		}
	}
}

func TestConflictError_FormatVariants(t *testing.T) {
	cases := []struct {
		err  ConflictError
		want string
	}{
		{ConflictError{Detail: "name already taken"}, "conflict: name already taken"},
		{ConflictError{}, "conflict"},
	}
	for _, c := range cases {
		if got := c.err.Error(); got != c.want {
			t.Errorf("ConflictError(%+v).Error() = %q, want %q", c.err, got, c.want)
		}
	}
}

func TestValidationError_FormatVariants(t *testing.T) {
	if got := (&ValidationError{Detail: "name is required"}).Error(); got != "validation error: name is required" {
		t.Errorf("got %q", got)
	}
	if got := (&ValidationError{}).Error(); got != "validation error" {
		t.Errorf("got %q", got)
	}
}

func TestAuthErrors_FormatVariants(t *testing.T) {
	if got := (&AuthenticationError{Detail: "token expired"}).Error(); got != "authentication failed: token expired" {
		t.Errorf("got %q", got)
	}
	if got := (&AuthorizationError{Detail: "needs admin"}).Error(); got != "authorization failed: needs admin" {
		t.Errorf("got %q", got)
	}
}

func TestAPIError_Format(t *testing.T) {
	got := (&APIError{StatusCode: 502, Body: "upstream barf"}).Error()
	want := "Terrapod API error (HTTP 502): upstream barf"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestIsNotFound(t *testing.T) {
	cases := []struct {
		err  error
		want bool
	}{
		{&NotFoundError{Resource: "workspace"}, true},
		{fmt.Errorf("wrap: %w", &NotFoundError{}), true}, // wrapping survives errors.As
		{&ConflictError{}, false},
		{errors.New("plain"), false},
		{nil, false},
	}
	for _, c := range cases {
		if got := IsNotFound(c.err); got != c.want {
			t.Errorf("IsNotFound(%v) = %v, want %v", c.err, got, c.want)
		}
	}
}

func TestIsConflictAndValidation(t *testing.T) {
	if !IsConflict(&ConflictError{Detail: "x"}) {
		t.Error("IsConflict should accept *ConflictError")
	}
	if !IsValidation(&ValidationError{Detail: "x"}) {
		t.Error("IsValidation should accept *ValidationError")
	}
	if IsConflict(&NotFoundError{}) {
		t.Error("IsConflict false-positive on NotFoundError")
	}
}

func TestIsAuth_CoversBothShapes(t *testing.T) {
	// IsAuth conflates 401 and 403 for display purposes — most callers
	// just want "auth-shaped" categorisation. Specific distinction is
	// still available via errors.As against the typed variants.
	if !IsAuth(&AuthenticationError{}) {
		t.Error("IsAuth should accept *AuthenticationError")
	}
	if !IsAuth(&AuthorizationError{}) {
		t.Error("IsAuth should accept *AuthorizationError")
	}
	if IsAuth(&NotFoundError{}) {
		t.Error("IsAuth false-positive on NotFoundError")
	}
}
