package terrapod

import (
	"context"
	"encoding/json"
)

// HAStatus is a node's own view of whether it is converging with its peer.
//
// Every field is answered from that node's local state — there is no call to
// the peer — so it stays fast and still works when the peer is the thing that
// has broken, which is when somebody is reading it.
//
// The two sides of a pair watch different fields:
//
//   - A follower watches SecondsSinceLastSync and BackfillingClasses. A node
//     mid-backfill is NOT in sync, however recent its last cycle.
//   - A leader watches OldestEventAgeSeconds against RetentionSeconds. As those
//     converge its follower is close to falling off the end of the retained
//     window and having to backfill from scratch.
//
// There is deliberately no "N events behind": that needs the peer's latest
// event id, and seconds-since-last-successful-pull is the more honest number.
// A completed pull that returned nothing means caught up as of then.
type HAStatus struct {
	NodeID             string `json:"node-id"`
	Role               string `json:"role"`
	PeerConfigured     bool   `json:"peer-configured"`
	ReplicationEnabled bool   `json:"replication-enabled"`

	// Follower side.
	LastSyncAt           string   `json:"last-sync-at,omitempty"`
	SecondsSinceLastSync int64    `json:"seconds-since-last-sync,omitempty"`
	BackfillingClasses   []string `json:"backfilling-classes,omitempty"`
	InSync               bool     `json:"in-sync"`

	// Leader side — the follower's margin before it must backfill.
	EventsRetained        int64    `json:"events-retained"`
	OldestEventAgeSeconds int64    `json:"oldest-event-age-seconds,omitempty"`
	RetentionSeconds      int64    `json:"retention-seconds"`
	ReplicatedClasses     []string `json:"replicated-classes,omitempty"`

	// In-cluster readiness — the other half of "is this HA". A pair that
	// replicates flawlessly is still not highly available if it serves from a
	// single API pod, which SingleReplicaComponents names directly.
	//
	// ComponentsUnavailableReason being set means the API could not read its
	// namespace — usually the operator declined the Role. That is "unknown",
	// not "nothing is running", so an empty Components with a reason set must
	// not be read as an outage.
	Components                  []ComponentReplicas `json:"components,omitempty"`
	ComponentsSampledAt         string              `json:"components-sampled-at,omitempty"`
	ComponentsUnavailableReason string              `json:"components-unavailable-reason,omitempty"`
	SingleReplicaComponents     []string            `json:"single-replica-components,omitempty"`
}

// ComponentReplicas is one Terrapod component's readiness in this namespace.
//
// Desired comes from the Deployment, not inferred from the pod list — without
// it, "1 ready" cannot be told apart from "1 of 3, mid-incident".
type ComponentReplicas struct {
	Name    string `json:"name"`
	Ready   int64  `json:"ready"`
	Desired int64  `json:"desired"`
}

// HANode is a node's identity and the role it currently holds.
type HANode struct {
	NodeID string `json:"node-id"`
	Role   string `json:"role"`
}

// GetHAStatus reports whether this node is converging with its peer.
//
// Requires admin or audit.
func (c *Client) GetHAStatus(ctx context.Context) (*HAStatus, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/ha/status")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(body)
	if err != nil {
		return nil, err
	}
	return &HAStatus{
		NodeID:                GetStringAttr(res, "node-id"),
		Role:                  GetStringAttr(res, "role"),
		PeerConfigured:        GetBoolAttr(res, "peer-configured"),
		ReplicationEnabled:    GetBoolAttr(res, "replication-enabled"),
		LastSyncAt:            GetStringAttr(res, "last-sync-at"),
		SecondsSinceLastSync:  GetIntAttr(res, "seconds-since-last-sync"),
		BackfillingClasses:    GetListAttr(res, "backfilling-classes"),
		InSync:                GetBoolAttr(res, "in-sync"),
		EventsRetained:        GetIntAttr(res, "events-retained"),
		OldestEventAgeSeconds: GetIntAttr(res, "oldest-event-age-seconds"),
		RetentionSeconds:      GetIntAttr(res, "retention-seconds"),
		ReplicatedClasses:     GetListAttr(res, "replicated-classes"),

		Components:                  parseComponents(res),
		ComponentsSampledAt:         GetStringAttr(res, "components-sampled-at"),
		ComponentsUnavailableReason: GetStringAttr(res, "components-unavailable-reason"),
		SingleReplicaComponents:     GetListAttr(res, "single-replica-components"),
	}, nil
}

func parseComponents(res *Resource) []ComponentReplicas {
	raw, ok := res.Attributes["components"]
	if !ok || len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var out []ComponentReplicas
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

// WhoAmI reports this node's identity and role.
//
// This is the endpoint a node probes against the shared DNS name to work out
// whether it owns that name: if the answer names itself, it is the leader. It
// is unauthenticated on the server, because the probe runs before any trust
// exists between the two nodes.
func (c *Client) WhoAmI(ctx context.Context) (*HANode, error) {
	body, err := c.Get(ctx, "/api/terrapod/v1/ha/whoami")
	if err != nil {
		return nil, err
	}
	res, err := ParseResource(body)
	if err != nil {
		return nil, err
	}
	return &HANode{
		NodeID: GetStringAttr(res, "node-id"),
		Role:   GetStringAttr(res, "role"),
	}, nil
}
