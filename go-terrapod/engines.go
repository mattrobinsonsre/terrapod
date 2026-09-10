package terrapod

import "context"

// ListEngines returns the engines this deployment enables ("terraform",
// "pulumi", …), sorted. Terraform is always present. An engine the deployment
// turns off is absent, not listed and refused (#1555).
func (c *Client) ListEngines(ctx context.Context) ([]string, error) {
	data, err := c.Get(ctx, "/api/v1/engines")
	if err != nil {
		return nil, err
	}
	resources, err := ParseResourceList(data)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(resources))
	for i := range resources {
		out = append(out, resources[i].ID)
	}
	return out, nil
}
