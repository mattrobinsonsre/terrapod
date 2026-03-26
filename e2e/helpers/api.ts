/**
 * Direct API helpers for E2E test setup.
 * Uses the API port (8000) directly, bypassing the BFF.
 */
import { createHash, randomBytes } from 'crypto';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

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
  const authRes = await fetch(`${API_URL}/api/v2/auth/local/authorize`, {
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
  const tokenRes = await fetch(`${API_URL}/api/v2/auth/token`, {
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
    `${API_URL}/api/v2/organizations/default/users`,
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
 * Create a workspace via the admin API. Returns the workspace ID.
 */
export async function createWorkspace(
  token: string,
  name: string,
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
          attributes: { name },
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
