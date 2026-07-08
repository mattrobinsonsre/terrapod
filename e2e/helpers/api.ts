/**
 * Direct API helpers for E2E test setup.
 * Uses the API port (8000) directly, bypassing the BFF.
 */
import { createHash, randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

/**
 * Read the session token out of a saved storageState file (e.g. admin.json),
 * so a spec can drive the API directly with the same identity its browser
 * context uses. Centralises the localStorage-extraction the specs used to
 * inline.
 */
export function getStoredToken(authFileName = 'admin.json'): string {
  const authPath = path.join(__dirname, '..', '.auth', authFileName);
  const authData = JSON.parse(fs.readFileSync(authPath, 'utf-8'));
  const origin = authData.origins?.find((o: { origin: string }) =>
    o.origin.includes('localhost'),
  );
  const entry = origin?.localStorage?.find(
    (e: { name: string }) => e.name === 'terrapod_auth',
  );
  return entry ? JSON.parse(entry.value).token : '';
}

/**
 * A process-unique, human-readable suffix for test resources. Within a shard
 * the workers share ONE stack/DB, so every test MUST name its resources
 * uniquely to avoid collisions — see the Code ↔ E2E Tests Contract in
 * CLAUDE.md. Combines a timestamp with random bytes so even same-millisecond
 * calls across workers don't collide.
 */
export function uniqueName(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
}

function generatePKCE() {
  const verifier = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  return { verifier, challenge };
}

/**
 * Obtain a session token via the local auth flow (API direct).
 */
export async function getSessionToken(
  email: string,
  password: string,
): Promise<{ token: string; roles: string[] }> {
  const { verifier, challenge } = generatePKCE();
  const state = randomBytes(16).toString('hex');

  // Step 1: authorize
  const authRes = await fetch(`${API_URL}/api/terrapod/v1/auth/local/authorize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email,
      password,
      code_challenge: challenge,
      code_challenge_method: 'S256',
      state,
    }),
  });

  if (!authRes.ok) {
    const body = await authRes.text();
    throw new Error(`Auth failed for ${email}: ${authRes.status} ${body}`);
  }

  const { code } = await authRes.json();

  // Step 2: token exchange
  const tokenRes = await fetch(`${API_URL}/api/terrapod/v1/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      code_verifier: verifier,
    }).toString(),
  });

  if (!tokenRes.ok) {
    const body = await tokenRes.text();
    throw new Error(`Token exchange failed: ${tokenRes.status} ${body}`);
  }

  const data = await tokenRes.json();
  return { token: data.session_token, roles: data.roles };
}

/**
 * Create a user via the admin API.
 */
export async function createUser(
  adminToken: string,
  email: string,
  password: string,
  displayName?: string,
): Promise<void> {
  const res = await fetch(
    `${API_URL}/api/terrapod/v1/users`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${adminToken}`,
      },
      body: JSON.stringify({
        data: {
          type: 'users',
          attributes: {
            email,
            password,
            'display-name': displayName || email.split('@')[0],
          },
        },
      }),
    },
  );

  if (!res.ok && res.status !== 409) {
    const body = await res.text();
    throw new Error(`Create user failed: ${res.status} ${body}`);
  }
}

/**
 * Set the platform/custom roles for a (provider, email) pair. Replaces any
 * existing assignments. Used in global setup to grant the audit user the
 * read-only `audit` role for RBAC negative tests.
 */
export async function setRoleAssignments(
  adminToken: string,
  email: string,
  roles: string[],
  providerName = 'local',
): Promise<void> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/role-assignments`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${adminToken}`,
    },
    body: JSON.stringify({
      data: {
        type: 'role-assignments',
        attributes: { 'provider-name': providerName, email, roles },
      },
    }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Set role assignments failed for ${email}: ${res.status} ${body}`);
  }
}

/**
 * Create a custom role with a granular capability set (#585). Levels are not
 * persisted — the role's grant IS its `capabilities`. `allowLabels` scopes the
 * label RBAC so the role only applies to workspaces carrying those labels.
 * Idempotent-ish: a 422 "already exists" is swallowed so re-runs don't fail.
 */
export async function createRole(
  adminToken: string,
  name: string,
  capabilities: string[],
  allowLabels: Record<string, string> = {},
  description = 'E2E capability role',
): Promise<void> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/roles`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${adminToken}`,
    },
    body: JSON.stringify({
      data: {
        name,
        type: 'roles',
        attributes: {
          description,
          capabilities,
          'allow-labels': allowLabels,
        },
      },
    }),
  });
  if (!res.ok && res.status !== 422) {
    const body = await res.text();
    throw new Error(`Create role failed: ${res.status} ${body}`);
  }
}

/**
 * Delete a custom role (teardown helper). A 404 is fine — already gone.
 */
export async function deleteRole(adminToken: string, name: string): Promise<void> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/roles/${name}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${adminToken}` },
  });
  if (!res.ok && res.status !== 404) {
    const body = await res.text();
    throw new Error(`Delete role failed: ${res.status} ${body}`);
  }
}

/**
 * Create a workspace via the admin API. Returns the workspace ID.
 */
export async function createWorkspace(
  token: string,
  name: string,
  attrs: Record<string, unknown> = {},
): Promise<string> {
  const res = await fetch(
    `${API_URL}/api/v2/organizations/default/workspaces`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        data: {
          type: 'workspaces',
          attributes: { name, ...attrs },
        },
      }),
    },
  );

  if (!res.ok) {
    // If the workspace already exists, fetch its ID
    if (res.status === 422) {
      const listRes = await fetch(
        `${API_URL}/api/v2/organizations/default/workspaces`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      const listData = await listRes.json();
      const existing = listData.data?.find(
        (ws: { attributes: { name: string } }) => ws.attributes.name === name,
      );
      if (existing) return existing.id;
    }
    const body = await res.text();
    throw new Error(`Create workspace failed: ${res.status} ${body}`);
  }

  const data = await res.json();
  return data.data.id;
}

/**
 * Create a registry module via the management API. Returns the bare module
 * UUID (usable directly as a catalog item's `module-id`).
 */
export async function createRegistryModule(
  token: string,
  name: string,
  provider = 'aws',
): Promise<string> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/registry-modules`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: { type: 'registry-modules', attributes: { name, provider } },
    }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Create registry module failed: ${res.status} ${body}`);
  }
  const data = await res.json();
  return data.data.id as string;
}

/**
 * Create an agent pool via the management API. Returns the bare pool UUID.
 */
export async function createAgentPool(
  token: string,
  name: string,
): Promise<string> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/agent-pools`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: { type: 'agent-pools', attributes: { name } },
    }),
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Create agent pool failed: ${res.status} ${body}`);
  }
  const data = await res.json();
  return data.data.id as string;
}

/**
 * Seed a run so the run-detail page can be exercised in E2E.
 *
 * The E2E stack has no runner/listener, so a seeded run simply sits in
 * `queued` — which is exactly what we want: the run-detail page renders in
 * full (status, tabs/picker, action row) without needing real execution. The
 * config-version tarball is never extracted here, so an arbitrary placeholder
 * blob is enough to satisfy the upload + let a run be created against it.
 *
 * Flow: create CV → PUT placeholder bytes (no auth, per the upload contract)
 * → POST /runs referencing the workspace's now-uploaded CV. Returns the
 * `run-<uuid>` id.
 */
export async function seedRun(
  token: string,
  workspaceId: string,
  planOnly = true,
): Promise<string> {
  const cvRes = await fetch(
    `${API_URL}/api/v2/workspaces/${workspaceId}/configuration-versions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        data: {
          type: 'configuration-versions',
          attributes: { 'auto-queue-runs': true },
        },
      }),
    },
  );
  if (!cvRes.ok) {
    throw new Error(`Create config version failed: ${cvRes.status} ${await cvRes.text()}`);
  }
  const cvId = (await cvRes.json()).data.id as string;

  // No Authorization header — the upload endpoint is unauthenticated (the CV
  // UUID is the capability token), matching go-tfe's presigned-style upload.
  const upRes = await fetch(
    `${API_URL}/api/v2/configuration-versions/${cvId}/upload`,
    { method: 'PUT', body: 'terrapod-e2e-config-placeholder' },
  );
  if (!upRes.ok) {
    throw new Error(`Config version upload failed: ${upRes.status}`);
  }

  const runRes = await fetch(`${API_URL}/api/v2/runs`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'runs',
        attributes: { 'plan-only': planOnly },
        relationships: {
          workspace: { data: { type: 'workspaces', id: workspaceId } },
        },
      },
    }),
  });
  if (!runRes.ok) {
    throw new Error(`Create run failed: ${runRes.status} ${await runRes.text()}`);
  }
  return (await runRes.json()).data.id as string;
}

/** Seed a workspace Terraform variable. Returns the variable id. */
export async function seedVariable(
  token: string,
  workspaceId: string,
  key: string,
  value = 'seed-value',
): Promise<string> {
  const res = await fetch(`${API_URL}/api/v2/workspaces/${workspaceId}/vars`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'vars',
        attributes: { key, value, category: 'terraform', sensitive: false, hcl: false },
      },
    }),
  });
  if (!res.ok) throw new Error(`Create variable failed: ${res.status} ${await res.text()}`);
  return (await res.json()).data.id as string;
}

/** Seed a workspace run task (enabled). Returns the run-task id. */
export async function seedRunTask(
  token: string,
  workspaceId: string,
  name: string,
  url = 'https://run-tasks.example.com/validate',
): Promise<string> {
  const res = await fetch(`${API_URL}/api/terrapod/v1/workspaces/${workspaceId}/run-tasks`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/vnd.api+json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      data: {
        type: 'run-tasks',
        attributes: { name, url, stage: 'post_plan', 'enforcement-level': 'mandatory', enabled: true },
      },
    }),
  });
  if (!res.ok) throw new Error(`Create run task failed: ${res.status} ${await res.text()}`);
  return (await res.json()).data.id as string;
}

/**
 * Seed a state-version record on a workspace (the record alone is enough to
 * render the State tab; content upload is not needed for a UI-render test).
 * Returns the state-version id.
 */
export async function seedStateVersion(
  token: string,
  workspaceId: string,
  serial = 1,
): Promise<string> {
  const res = await fetch(
    `${API_URL}/api/v2/workspaces/${workspaceId}/state-versions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        data: {
          type: 'state-versions',
          attributes: {
            serial,
            md5: 'd41d8cd98f00b204e9800998ecf8427e',
            lineage: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
          },
        },
      }),
    },
  );
  if (!res.ok) {
    throw new Error(`Create state version failed: ${res.status} ${await res.text()}`);
  }
  return (await res.json()).data.id as string;
}

/**
 * Wait for the stack to be healthy by polling the API ping endpoint.
 */
export async function waitForStack(timeoutMs = 120_000): Promise<void> {
  const start = Date.now();
  const url = `${BASE_URL}/api/v2/ping`;

  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(5_000) });
      if (res.ok) return;
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }

  throw new Error(`Stack not healthy after ${timeoutMs}ms`);
}
