// SessionStorage keys used during the PKCE auth flow.
// Shared between login/page.tsx (write) and auth/callback (read + cleanup).
export const STORAGE_PKCE_VERIFIER = 'terrapod_pkce_verifier'
export const STORAGE_AUTH_STATE = 'terrapod_auth_state'
export const STORAGE_REDIRECT_AFTER_LOGIN = 'terrapod_redirect_after_login'
