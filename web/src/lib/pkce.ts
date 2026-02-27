/**
 * PKCE (Proof Key for Code Exchange) helpers for OAuth2 flows.
 */

export function base64url(bytes: Uint8Array): string {
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export async function generatePKCE(): Promise<{ verifier: string; challenge: string }> {
  const array = new Uint8Array(32)
  crypto.getRandomValues(array)
  const verifier = base64url(array)

  const encoded = new TextEncoder().encode(verifier)
  const digest = await crypto.subtle.digest('SHA-256', encoded)
  const challenge = base64url(new Uint8Array(digest))

  return { verifier, challenge }
}

export function generateState(): string {
  const array = new Uint8Array(16)
  crypto.getRandomValues(array)
  return base64url(array)
}
