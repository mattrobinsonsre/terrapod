import { test, expect } from '@playwright/test';
import { randomBytes } from 'crypto';
import {
  getStoredToken,
  getSessionToken,
  createUser,
  createRole,
  deleteRole,
  setRoleAssignments,
  createWorkspace,
} from '../helpers/api.js';

/**
 * Capability RBAC enforcement (#585) — the headline of the capability model is
 * a grant that hierarchical LEVELS could never express: "plan but NOT apply".
 * This spec proves that granularity is enforced SERVER-SIDE through the full
 * BFF proxy chain (CDN → ingress → BFF → API), not just that the authoring UI
 * renders a checkbox.
 *
 * A services-tier test with a mocked DB proves the gate *calls* the resolver;
 * only this end-to-end path proves a real capability-scoped session is actually
 * blocked at the run endpoint through every real layer — the E2E half of the
 * RBAC contract.
 *
 * Setup uses the admin token (from storageState) to mint a granular role, a
 * label-matched workspace, a user, and the assignment. Enforcement is then
 * asserted with the capability user's OWN session token, driven through the BFF
 * (BASE_URL), never the direct API port.
 */

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

test.describe('Capability RBAC — plan-but-not-apply enforced through the BFF', () => {
  // The granular grant: read + plan on the matching workspaces, but NOT apply
  // and NOT delete. This is not any single level preset — it's the whole point
  // of capabilities.
  const CAPS = ['workspace:read', 'run:read', 'run:plan', 'var:read'];

  // Hyphen-free unique token so resource names satisfy strict name patterns and
  // the URLs need no post-hoc sanitising.
  const tok = `cap585${randomBytes(5).toString('hex')}`;
  const roleName = `role${tok}`;
  const wsName = `ws${tok}`;
  const labelKey = `cap585key${randomBytes(3).toString('hex')}`;
  const userEmail = `${tok}@terrapod.local`;
  const userPassword = 'TestPassword123!';
  const wsByName = `${BASE_URL}/api/v2/organizations/default/workspaces/${wsName}`;

  let adminToken = '';
  let wsId = '';
  let userToken = '';
  // Workspace delete is a Terrapod-native, by-ID route (`workspace:delete`
  // capability) — NOT the org-scoped TFE-V2 GET-by-name path, which has no
  // DELETE verb (405). Built once wsId is known.
  let wsDeleteUrl = '';

  test.beforeAll(async () => {
    adminToken = getStoredToken('admin.json');

    // Granular role scoped by a unique label so it only applies to our workspace.
    await createRole(adminToken, roleName, CAPS, { [labelKey]: 'yes' });

    // Label-matched workspace (agent mode so a run can be queued at all).
    wsId = await createWorkspace(adminToken, wsName, {
      'execution-mode': 'agent',
      labels: { [labelKey]: 'yes' },
    });
    wsDeleteUrl = `${BASE_URL}/api/terrapod/v1/workspaces/${wsId}`;

    // A fresh principal holding ONLY the granular role (plus implicit everyone).
    await createUser(adminToken, userEmail, userPassword, 'Cap585 User');
    await setRoleAssignments(adminToken, userEmail, [roleName]);

    const session = await getSessionToken(userEmail, userPassword);
    userToken = session.token;
    expect(userToken).toBeTruthy();
  });

  test.afterAll(async () => {
    // Best-effort teardown of the resources this spec created (admin deletes via
    // the by-ID Terrapod-native route, which the capability user was blocked from).
    if (wsDeleteUrl) {
      await fetch(wsDeleteUrl, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${adminToken}` },
      }).catch(() => {});
    }
    await setRoleAssignments(adminToken, userEmail, []).catch(() => {});
    await deleteRole(adminToken, roleName).catch(() => {});
  });

  async function postRun(planOnly: boolean): Promise<Response> {
    return fetch(`${BASE_URL}/api/v2/runs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${userToken}`,
      },
      body: JSON.stringify({
        data: {
          type: 'runs',
          attributes: { 'plan-only': planOnly },
          relationships: {
            workspace: { data: { type: 'workspaces', id: wsId } },
          },
        },
      }),
    });
  }

  test('granular principal CAN read its label-matched workspace', async () => {
    const res = await fetch(wsByName, {
      headers: { Authorization: `Bearer ${userToken}` },
    });
    expect(res.status).toBe(200);
  });

  test('apply run is BLOCKED — lacks run:apply (403)', async () => {
    const res = await postRun(false);
    expect(res.status).toBe(403);
    const body = await res.json();
    // The gate names the missing capability, not a level threshold.
    expect(JSON.stringify(body)).toContain('run:apply');
  });

  test('plan-only run is ALLOWED past the RBAC gate — has run:plan (not 403)', async () => {
    const res = await postRun(true);
    // The principal HOLDS run:plan, so the capability gate must not block it.
    // The workspace has no configuration uploaded, so the request fails LATER
    // (422 "no configuration") or succeeds (201) — either proves the gate was
    // passed. The one status we must never see here is 403.
    expect(res.status).not.toBe(403);
    expect([201, 422]).toContain(res.status);
  });

  test('workspace delete is BLOCKED — lacks workspace:delete (403)', async () => {
    // Delete is the by-ID Terrapod-native route (`workspace:delete`); the
    // org-scoped TFE-V2 name path has no DELETE verb.
    const res = await fetch(wsDeleteUrl, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${userToken}` },
    });
    expect(res.status).toBe(403);
    const body = await res.json();
    expect(JSON.stringify(body)).toContain('workspace:delete');
  });
});
