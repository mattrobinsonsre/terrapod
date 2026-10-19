package terrapod

import (
	"context"
	"encoding/json"
	"fmt"
)

// Ansible execution environments: which ones this deployment can run (#2154).
//
// A configure operation runs its playbooks inside a container image carrying
// one ansible-core line. There are two kinds and the difference is who vouches
// for the image:
//
//   - KindBuiltIn: one tag per ansible-core line in the deployment's configured
//     EE repository, selected by a workspace's AnsibleVersion. Terrapod builds,
//     smokes, scans and republishes these.
//   - KindOperator: an image the operator added under a label, selected by a
//     workspace's AnsibleExecutionEnvironment -- which OVERRIDES AnsibleVersion
//     when set. Trusted code, used exactly as given.
//
// # ListingUnknown is not the same as an empty list
//
// The built-in set is read from a registry, so it can be unavailable. When it
// is, Listing is ListingUnknown and the server ACCEPTS any ansible-core line on
// a workspace write rather than refusing one it cannot confirm -- otherwise an
// unreachable registry would refuse ordinary workspace edits.
//
// So a caller offering a picker should let someone type a line when the listing
// is unknown, rather than presenting an empty list and implying there is
// nothing to choose. Treating unknown as empty is the one way to get this
// wrong, and it fails in the direction that blocks legitimate work.

// Execution-environment kinds, as returned in the `kind` attribute.
const (
	KindBuiltIn  = "built-in"
	KindOperator = "operator"
)

// Listing states for a set of execution environments.
const (
	// ListingComplete: the registry answered. An absent line does not exist,
	// and the server will refuse a workspace naming it.
	ListingComplete = "complete"
	// ListingUnknown: the set could not be determined. The server accepts any
	// line; do not present this as "none available".
	ListingUnknown = "unknown"
)

// AnsibleExecutionEnvironment is one selectable environment.
type AnsibleExecutionEnvironment struct {
	// ID is the ansible-core line for a built-in, or the label for an
	// operator-supplied one -- whichever a workspace would be set to.
	ID string
	// Kind is KindBuiltIn or KindOperator.
	Kind string
	// AnsibleCoreLine is set for a built-in and empty for an operator
	// environment, whose ansible-core version is the operator's business.
	AnsibleCoreLine string
	// Label is set for an operator environment and empty for a built-in.
	Label string
	// Image is the reference a run would execute in, resolved by the server's
	// own resolver -- so it cannot drift from what actually runs.
	Image string
}

// AnsibleExecutionEnvironments is a listing plus whether it is authoritative.
type AnsibleExecutionEnvironments struct {
	Environments []AnsibleExecutionEnvironment
	// Listing is ListingComplete or ListingUnknown. Check it before treating an
	// empty Environments as "this deployment can run nothing".
	Listing string
}

// ListAnsibleExecutionEnvironments returns the environments a workspace may be
// pointed at, built-in and operator-supplied together.
//
// Both kinds come from one call deliberately: the precedence between them is
// not symmetric, so merging two listings is a thing every caller would have to
// get right separately.
func (c *Client) ListAnsibleExecutionEnvironments(
	ctx context.Context,
) (*AnsibleExecutionEnvironments, error) {
	data, err := c.Get(ctx, "/api/v1/ansible/execution-environments")
	if err != nil {
		return nil, err
	}
	var out struct {
		Data []struct {
			ID         string `json:"id"`
			Attributes struct {
				Kind            string `json:"kind"`
				AnsibleCoreLine string `json:"ansible-core-line"`
				Label           string `json:"label"`
				Image           string `json:"image"`
			} `json:"attributes"`
		} `json:"data"`
		Meta struct {
			Listing string `json:"listing"`
		} `json:"meta"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("parse ansible execution environments: %w", err)
	}

	result := &AnsibleExecutionEnvironments{
		Environments: make([]AnsibleExecutionEnvironment, 0, len(out.Data)),
		Listing:      out.Meta.Listing,
	}
	if result.Listing == "" {
		// A server predating `meta.listing` cannot tell us, and assuming the
		// listing is authoritative is the unsafe default: it would present an
		// empty set as "nothing available" on a deployment where the registry
		// was simply unreachable.
		result.Listing = ListingUnknown
	}
	for _, d := range out.Data {
		result.Environments = append(result.Environments, AnsibleExecutionEnvironment{
			ID:              d.ID,
			Kind:            d.Attributes.Kind,
			AnsibleCoreLine: d.Attributes.AnsibleCoreLine,
			Label:           d.Attributes.Label,
			Image:           d.Attributes.Image,
		})
	}
	return result, nil
}
