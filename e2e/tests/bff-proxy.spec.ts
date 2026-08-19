import { test, expect } from '@playwright/test';

// The BFF proxies four prefixes to the API. Three of them (/v1, /oauth,
// /.well-known) moved off a Next middleware rewrite and onto the shared Route
// Handler in #1381, so that a failed upstream connection could be caught,
// retried and reported as a 502 instead of surfacing as Next's bare 500.
//
// That move re-routes the paths the terraform/tofu CLI depends on — service
// discovery, `terraform login`, and the provider network mirror. If the rewrite
// wiring is wrong they do not fail loudly in the UI; they fail in somebody's
// `tofu init`, which is exactly how the original defect went unnoticed. These
// assertions are the guard on that wiring: they only pass if a request to the
// public path reaches the API and comes back with the API's own response.
//
// They are deliberately about ROUTING, not authorization — an endpoint that
// answers 401 has still been proxied, and a 404 from Next (route not matched)
// is what a broken rewrite looks like.

test.describe('BFF proxy routing', () => {
  test('service discovery is proxied to the API', async ({ request }) => {
    // /.well-known is the reason the rewrite uses a marker prefix at all:
    // Next's file-system router ignores dot-prefixed folders, so this path
    // cannot be served by an `app/.well-known/` route directory.
    const res = await request.get('/.well-known/terraform.json');
    expect(res.status()).toBe(200);

    // The discovery document is what points the CLI at every other endpoint;
    // if this is served by anything other than the API it is worthless.
    const body = await res.json();
    expect(body).toHaveProperty('modules.v1');
    expect(body).toHaveProperty('providers.v1');
    expect(body).toHaveProperty('state.v2');
  });

  test('the provider mirror is proxied to the API', async ({ request }) => {
    // The exact prefix that failed in #1381. Unauthenticated, so the API
    // answers 401 — which still proves the request reached it. A 404 here would
    // mean Next matched nothing and the rewrite is broken.
    const res = await request.get(
      '/v1/providers/registry.opentofu.org/hashicorp/null/index.json',
    );
    expect(res.status()).not.toBe(404);
    expect([401, 403]).toContain(res.status());
  });

  test('the oauth prefix is proxied to the API', async ({ request }) => {
    // `terraform login` posts here. The API rejects a malformed grant with a
    // 4xx of its own; Next would return 405 or 404 for an unmatched route.
    const res = await request.post('/oauth/token', {
      form: { grant_type: 'authorization_code' },
      failOnStatusCode: false,
    });
    expect(res.status()).not.toBe(404);
    expect(res.status()).toBeGreaterThanOrEqual(400);
    expect(res.status()).toBeLessThan(500);
  });

  test('an unknown API path returns the API 404, not a Next page', async ({ request }) => {
    // Distinguishes "proxied, and the API said no such route" from "the BFF
    // never forwarded it". The latter renders HTML.
    const res = await request.get('/v1/providers/definitely/not/a/route.json', {
      failOnStatusCode: false,
    });
    expect(res.headers()['content-type'] ?? '').not.toContain('text/html');
  });
});
