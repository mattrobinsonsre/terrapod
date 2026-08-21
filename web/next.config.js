/** @type {import('next').NextConfig} */

// next-intl (#767) — the plugin points at the per-request locale/message
// resolver (src/i18n/request.ts). We use next-intl WITHOUT i18n routing (no
// `/[locale]/` URL segment): the locale comes from a cookie, so every existing
// route + BFF path is untouched.
const createNextIntlPlugin = require('next-intl/plugin')
const withNextIntl = createNextIntlPlugin('./src/i18n/request.ts')

// HSTS (Strict-Transport-Security). Tells browsers to remember "always
// use HTTPS for THIS hostname" for max-age, so subsequent visits to
// http://hostname auto-upgrade to https:// without trying port 80.
// Set via the HSTS env var at runtime (the chart populates it from
// `web.hsts.value`); default below is 2 years.
//
// NO `includeSubDomains`: Terrapod's HSTS assertion is scoped to the
// hostname it's served at. The hostname's parent zone (e.g. `ts.net`
// for Tailscale-hosted deployments, or a corporate-internal zone) is
// almost always shared with other services that have their own HTTP
// policies — asserting `includeSubDomains` would force HTTPS on every
// sibling host the browser later visits, which is the deploying
// operator's call, not ours. Operators who DO own the entire parent
// zone can set `web.hsts.value` to include the directive explicitly.
//
// NO `preload`: that's a one-way commitment to the hsts-preload list
// at https://hstspreload.org — also operator's call.
//
// Set HSTS="" to disable the header entirely (e.g. mixed http/https
// deployments).
const HSTS_DEFAULT = 'max-age=63072000'
const hstsValue = process.env.HSTS ?? HSTS_DEFAULT

const nextConfig = {
  output: 'standalone',
  allowedDevOrigins: ['terrapod.local'],
  // Do not redirect `/path/` to `/path` (#1408).
  //
  // Next normalises trailing slashes *before* rewrites, and the OCI
  // distribution spec's version check is literally `GET /v2/` — so the one
  // endpoint a registry client uses to decide whether this host speaks the API
  // was answered with a 308 to `/v2`, which is not a path the spec defines. A
  // redirect there is not a cosmetic difference: it is the handshake, and some
  // clients drop credentials across one.
  //
  // The cost is that page URLs are no longer normalised, which is why this is
  // the setting rather than `trailingSlash`: the app's own links carry no
  // trailing slash, so nothing in the UI depends on the redirect existing.
  skipTrailingSlashRedirect: true,
  // Route the BFF's non-/api prefixes onto the shared proxy Route Handler
  // (#1381). These are INTERNAL rewrites — they do not name the API, they name
  // a route in this process — so unlike a rewrite pointing at API_URL they are
  // safe to bake at build time (#47 does not apply).
  //
  // Why not three sibling route directories: Next's file-system router ignores
  // dot-prefixed folders, so `app/.well-known/` would never match. One marker
  // prefix handles all three uniformly.
  //
  // The marker is `/bff`, NOT `/_bff`: a leading underscore marks a PRIVATE
  // folder in the App Router, so `app/_bff/` is excluded from routing entirely
  // and every rewrite would 404 — with a green build, because nothing checks
  // that a rewrite destination resolves. The handler refuses any path that did
  // not come from one of the sources below, so `/bff` is not a usable alias.
  //
  // `beforeFiles` so these win before any page route is considered.
  async rewrites() {
    return {
      beforeFiles: [
        { source: '/.well-known/:path*', destination: '/bff/.well-known/:path*' },
        { source: '/oauth/:path*', destination: '/bff/oauth/:path*' },
        { source: '/v1/:path*', destination: '/bff/v1/:path*' },
        // The OCI registry (#1408). `/v2/` is not a path Terrapod chose — the
        // distribution spec mandates that prefix — so it has to be proxied like
        // any other API surface, or `docker pull` against the deployment's own
        // hostname reaches the web pod and gets an HTML 404. It streams through
        // the Route Handler, which matters more here than anywhere else: image
        // layers are the largest bodies Terrapod moves.
        { source: '/v2/:path*', destination: '/bff/v2/:path*' },
      ],
    }
  },
  // Prevent gzip buffering on SSE endpoints. Next.js compression buffers
  // small messages (keepalives, events) indefinitely, breaking real-time
  // streaming. Setting Content-Encoding: none tells the compression
  // middleware to pass these responses through unmodified.
  //
  // SSE endpoints are all Terrapod-native at /api/terrapod/v1. The
  // transitional /api/v2 aliases (#269) were removed in v0.24.0 (#278).
  async headers() {
    const headers = [
      {
        source: '/api/terrapod/v1/listeners/:path*',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/workspaces/:path*/runs/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/workspace-events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
      {
        source: '/api/terrapod/v1/agent-pools/:path*/events',
        headers: [{ key: 'Content-Encoding', value: 'none' }],
      },
    ]
    if (hstsValue) {
      headers.push({
        source: '/:path*',
        headers: [{ key: 'Strict-Transport-Security', value: hstsValue }],
      })
    }
    return headers
  },
}

module.exports = withNextIntl(nextConfig)
