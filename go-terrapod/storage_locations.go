package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// StorageLocation is a named object store a workspace may be assigned to
// (#2547).
//
// Names and backends only, which is what the API serves. A location's bucket,
// region, endpoint and credentials are deployment configuration and are not
// exposed: nothing a caller does with this type needs them, and the credential
// fields sit beside the addressing fields in the server's config model.
type StorageLocation struct {
	// Name is the value to send as a workspace's `storage-location`.
	Name string
	// Backend is "s3", "azure" or "gcs". A location never uses the filesystem
	// backend -- the server refuses that configuration, because the presigned
	// routes for it use one process-global store.
	Backend string
}

// ListStorageLocations returns the named storage locations this deployment has.
//
// An empty slice is the ordinary answer for a single-store deployment, which is
// every deployment that has not configured `storage.locations`. Treat it as
// "offer no choice" rather than as an error.
//
// `default` is deliberately absent. It is the ABSENCE of a location rather than
// a location, and the server refuses the literal string -- so a caller
// presenting a choice supplies its own "use the default" option mapping to a
// null `storage-location`, and must not send "default".
func (c *Client) ListStorageLocations(ctx context.Context) ([]StorageLocation, error) {
	data, err := c.Get(ctx, "/api/v1/storage/locations")
	if err != nil {
		return nil, err
	}
	var out struct {
		Data []struct {
			ID         string `json:"id"`
			Attributes struct {
				Name    string `json:"name"`
				Backend string `json:"backend"`
			} `json:"attributes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse storage locations: %w", err)
	}

	locations := make([]StorageLocation, 0, len(out.Data))
	for _, d := range out.Data {
		name := d.Attributes.Name
		if name == "" {
			// The id is the name, so a server that stops sending the attribute
			// still yields a usable value rather than a slice of blanks.
			name = d.ID
		}
		locations = append(locations, StorageLocation{Name: name, Backend: d.Attributes.Backend})
	}
	return locations, nil
}
