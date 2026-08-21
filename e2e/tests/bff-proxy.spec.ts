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
    // if this is served by anything other than the API it is worthless. Index
    // the keys directly rather than via toHaveProperty — its dotted argument is
    // a PATH, so `toHaveProperty('modules.v1')` looks for `body.modules.v1`,
    // and every key here legitimately contains a dot.
    const body = await res.json();
    expect(body['tfe.v2']).toBe('/api/v2/');
    expect(body['modules.v1']).toBe('/api/v2/registry/modules/');
    expect(body['providers.v1']).toBe('/api/v2/registry/providers/');
    // `terraform login` reads its endpoints from here, and they are on another
    // of the prefixes this change moved.
    expect(body['login.v1']).toMatchObject({
      authz: '/oauth/authorize',
      token: '/oauth/token',
    });
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

  test('the OCI registry is proxied to the API', async ({ request }) => {
    // `/v2/` is the registry's version check — the endpoint a container client
    // uses to decide whether this host speaks the distribution API at all. The
    // prefix is mandated by the spec, so it cannot be moved under /api, and a
    // deployment routes every ingress path to the BFF: unproxied, `docker pull`
    // against the deployment's own hostname reaches the web pod and gets an
    // HTML 404 (#1408).
    //
    // Unauthenticated, so the API answers 401 — which still proves the request
    // reached it, and proves the registry is not open to the world.
    const res = await request.get('/v2/', { failOnStatusCode: false });
    expect(res.status()).toBe(401);
    expect(res.headers()['content-type'] ?? '').not.toContain('text/html');
    // The spec's error envelope, so this is the registry answering and not some
    // other 401 on the way.
    expect((await res.json()).errors[0].code).toBe('UNAUTHORIZED');
  });

  test('/v2/ is not trailing-slash redirected', async ({ request }) => {
    // Next normalises `/path/` to `/path` before rewrites run, which turned the
    // registry handshake into a 308 to a path the spec does not define. Some
    // clients drop credentials across a redirect, so this is a correctness
    // requirement rather than a tidiness one.
    const res = await request.get('/v2/', { maxRedirects: 0, failOnStatusCode: false });
    expect(res.status()).not.toBe(308);
    expect(res.status()).not.toBe(307);
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
