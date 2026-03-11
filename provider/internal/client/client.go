package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"
)

// Client is the Terrapod API HTTP client.
type Client struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

// NewClient creates a new Terrapod API client.
func NewClient(hostname, token string, skipTLSVerify bool) *Client {
	scheme := "https"
	if strings.HasPrefix(hostname, "http://") || strings.HasPrefix(hostname, "https://") {
		// Already has scheme — use as-is
		scheme = ""
	}

	baseURL := hostname
	if scheme != "" {
		baseURL = scheme + "://" + hostname
	}
	baseURL = strings.TrimSuffix(baseURL, "/")

	transport := &http.Transport{}
	if skipTLSVerify {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec
	}

	return &Client{
		BaseURL: baseURL,
		Token:   token,
		HTTPClient: &http.Client{
			Transport: transport,
			Timeout:   30 * time.Second,
		},
	}
}

// do performs an HTTP request with retry on 429/5xx.
func (c *Client) do(ctx context.Context, method, path string, body []byte) ([]byte, int, error) {
	var lastErr error
	maxRetries := 3

	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(math.Pow(2, float64(attempt))) * time.Second
			select {
			case <-ctx.Done():
				return nil, 0, ctx.Err()
			case <-time.After(backoff):
			}
		}

		url := c.BaseURL + path

		var bodyReader io.Reader
		if body != nil {
			bodyReader = bytes.NewReader(body)
		}

		req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
		if err != nil {
			return nil, 0, fmt.Errorf("creating request: %w", err)
		}

		req.Header.Set("Authorization", "Bearer "+c.Token)
		req.Header.Set("Content-Type", "application/vnd.api+json")
		req.Header.Set("Accept", "application/json")

		resp, err := c.HTTPClient.Do(req)
		if err != nil {
			lastErr = err
			continue
		}

		respBody, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return nil, resp.StatusCode, fmt.Errorf("reading response: %w", err)
		}

		// Retry on 429 or 5xx
		if resp.StatusCode == 429 || resp.StatusCode >= 500 {
			lastErr = &APIError{StatusCode: resp.StatusCode, Body: string(respBody)}
			continue
		}

		return respBody, resp.StatusCode, nil
	}

	return nil, 0, fmt.Errorf("request failed after %d retries: %w", maxRetries, lastErr)
}

// handleError converts HTTP error status codes into typed errors.
func handleError(statusCode int, body []byte) error {
	detail := extractErrorDetail(body)

	switch statusCode {
	case http.StatusNotFound:
		return &NotFoundError{Resource: "resource", ID: ""}
	case http.StatusConflict:
		return &ConflictError{Detail: detail}
	case http.StatusUnprocessableEntity:
		return &ValidationError{Detail: detail}
	default:
		return &APIError{StatusCode: statusCode, Body: detail}
	}
}

// extractErrorDetail tries to extract a human-readable message from a JSON:API error response.
func extractErrorDetail(body []byte) string {
	var errResp ErrorResponse
	if err := json.Unmarshal(body, &errResp); err == nil && len(errResp.Errors) > 0 {
		parts := make([]string, 0, len(errResp.Errors))
		for _, e := range errResp.Errors {
			if e.Detail != "" {
				parts = append(parts, e.Detail)
			} else if e.Title != "" {
				parts = append(parts, e.Title)
			}
		}
		if len(parts) > 0 {
			return strings.Join(parts, "; ")
		}
	}
	return string(body)
}

// Get performs a GET request and returns the response body.
func (c *Client) Get(ctx context.Context, path string) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, handleError(status, body)
	}
	return body, nil
}

// Post performs a POST request and returns the response body.
func (c *Client) Post(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPost, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, handleError(status, body)
	}
	return body, nil
}

// Patch performs a PATCH request and returns the response body.
func (c *Client) Patch(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPatch, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, handleError(status, body)
	}
	return body, nil
}

// Delete performs a DELETE request.
func (c *Client) Delete(ctx context.Context, path string) error {
	body, status, err := c.do(ctx, http.MethodDelete, path, nil)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return handleError(status, body)
	}
	return nil
}

// DeleteWithBody performs a DELETE request with a JSON body.
func (c *Client) DeleteWithBody(ctx context.Context, path string, payload []byte) error {
	body, status, err := c.do(ctx, http.MethodDelete, path, payload)
	if err != nil {
		return err
	}
	if status < 200 || status >= 300 {
		return handleError(status, body)
	}
	return nil
}

// Put performs a PUT request and returns the response body.
func (c *Client) Put(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, status, err := c.do(ctx, http.MethodPut, path, payload)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, handleError(status, body)
	}
	return body, nil
}

// IsNotFound returns true if the error is a NotFoundError.
func IsNotFound(err error) bool {
	_, ok := err.(*NotFoundError)
	return ok
}
