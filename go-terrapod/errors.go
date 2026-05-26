package terrapod

import (
	"errors"
	"fmt"
)

// NotFoundError indicates the requested resource does not exist (HTTP
// 404). Tests use errors.As / errors.Is to react to this case
// (creating a missing resource, returning nil from a data source, etc).
//
//	var nf *terrapod.NotFoundError
//	if errors.As(err, &nf) { ... }
type NotFoundError struct {
	// Resource is the resource type the caller was looking for —
	// "workspace", "variable", "agent-pool". Empty when the caller
	// didn't supply one.
	Resource string
	// ID is the canonical id ("ws-abc123") the caller used. Empty
	// when the request wasn't id-keyed (e.g. a list with a filter
	// that matched zero rows is not a NotFoundError).
	ID string
}

func (e *NotFoundError) Error() string {
	if e.Resource != "" && e.ID != "" {
		return fmt.Sprintf("%s %q not found", e.Resource, e.ID)
	}
	if e.Resource != "" {
		return fmt.Sprintf("%s not found", e.Resource)
	}
	return "resource not found"
}

// ConflictError indicates a state conflict (HTTP 409). Common causes:
// creating a workspace whose name is already taken; locking a
// workspace that's already locked.
type ConflictError struct {
	// Detail is the human-readable message from the Terrapod
	// JSON:API error object (decoded). Empty when the response body
	// didn't include one.
	Detail string
}

func (e *ConflictError) Error() string {
	if e.Detail == "" {
		return "conflict"
	}
	return "conflict: " + e.Detail
}

// ValidationError indicates invalid input (HTTP 422). The Detail
// carries the per-field message Terrapod produced — surface it
// verbatim to the operator.
type ValidationError struct {
	Detail string
}

func (e *ValidationError) Error() string {
	if e.Detail == "" {
		return "validation error"
	}
	return "validation error: " + e.Detail
}

// AuthenticationError indicates the token was rejected (HTTP 401).
// Distinct from a generic APIError so callers can prompt for
// re-authentication rather than generic retry.
type AuthenticationError struct {
	Detail string
}

func (e *AuthenticationError) Error() string {
	if e.Detail == "" {
		return "authentication failed"
	}
	return "authentication failed: " + e.Detail
}

// AuthorizationError indicates the token authenticated but lacks the
// required permission (HTTP 403). Operator action is typically
// "ask an admin for the role" rather than "rotate the token".
type AuthorizationError struct {
	Detail string
}

func (e *AuthorizationError) Error() string {
	if e.Detail == "" {
		return "authorization failed"
	}
	return "authorization failed: " + e.Detail
}

// APIError is the catch-all for HTTP statuses without a more specific
// typed error above. Callers can still recover meaningful info from
// StatusCode + Body for diagnostics.
type APIError struct {
	// StatusCode is the HTTP status returned.
	StatusCode int
	// Body is either the decoded JSON:API error detail (when one was
	// present in the response) or the raw response body (otherwise),
	// surfaced verbatim so operators see exactly what Terrapod said.
	Body string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("Terrapod API error (HTTP %d): %s", e.StatusCode, e.Body)
}

// IsNotFound returns true if err (or any error in its chain) is a
// *NotFoundError. Use this for the common "create the resource if it
// doesn't exist" idiom without dragging errors.As into every caller.
//
//	ws, err := client.GetWorkspace(ctx, "ws-abc")
//	if terrapod.IsNotFound(err) {
//	    ws, err = client.CreateWorkspace(ctx, req)
//	}
func IsNotFound(err error) bool {
	var nf *NotFoundError
	return errors.As(err, &nf)
}

// IsConflict returns true if err is a *ConflictError. Useful for
// "already exists" idioms where the caller wants to fetch-or-create.
func IsConflict(err error) bool {
	var ce *ConflictError
	return errors.As(err, &ce)
}

// IsValidation returns true if err is a *ValidationError.
func IsValidation(err error) bool {
	var ve *ValidationError
	return errors.As(err, &ve)
}

// IsAuth returns true if err is an authentication OR authorization
// error (HTTP 401 OR 403). Most callers care about the distinction
// only at error-display time; rotating-the-token UX cares only about
// "anything auth-shaped".
func IsAuth(err error) bool {
	var (
		ae *AuthenticationError
		ze *AuthorizationError
	)
	return errors.As(err, &ae) || errors.As(err, &ze)
}
