package client

import "fmt"

// NotFoundError indicates the requested resource does not exist.
type NotFoundError struct {
	Resource string
	ID       string
}

func (e *NotFoundError) Error() string {
	if e.ID != "" {
		return fmt.Sprintf("%s %q not found", e.Resource, e.ID)
	}
	return fmt.Sprintf("%s not found", e.Resource)
}

// ConflictError indicates a state conflict (e.g. duplicate name).
type ConflictError struct {
	Detail string
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("conflict: %s", e.Detail)
}

// ValidationError indicates invalid input (422).
type ValidationError struct {
	Detail string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("validation error: %s", e.Detail)
}

// APIError is a generic API error with status code.
type APIError struct {
	StatusCode int
	Body       string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("API error (HTTP %d): %s", e.StatusCode, e.Body)
}
