'use client';

import { useEffect } from 'react';

import { isChunkError } from '@/lib/chunk-error';

// Next.js renders this (replacing the root layout) when an error escapes every
// nested boundary — including a failed chunk/RSC fetch after a new deploy, where
// the browser still holds the previous build's chunk hashes (see #847). In that
// case a full reload pulls the current build and self-heals, so we reload once —
// guarded against a loop — instead of dead-ending on Next's default error page.
//
// This component renders OUTSIDE the app's providers and global CSS (it replaces
// the root layout), so it cannot use next-intl and must inline its styles. The
// English copy is the deliberate last-resort fallback (only seen if the one-shot
// reload guard trips); the user-facing literals carry `i18n-ignore`.

const RELOAD_GUARD_KEY = 'tp:chunk-reload-at';
const RELOAD_WINDOW_MS = 10_000;

export default function GlobalError({
  error,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    if (!isChunkError(error)) return;
    // Reload at most once per short window: if the chunk is genuinely gone
    // (not just a stale-deploy race) a blind reload would loop forever.
    try {
      const last = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
      if (Date.now() - last > RELOAD_WINDOW_MS) {
        sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()));
        window.location.reload();
      }
    } catch {
      // sessionStorage unavailable (private mode / disabled) — fall through to
      // the manual reload UI rather than risk a loop.
    }
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: '100dvh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#0f172a',
          color: '#e2e8f0',
          fontFamily:
            'Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
        }}
      >
        <div style={{ textAlign: 'center', padding: '2rem', maxWidth: 420 }}>
          <div style={{ fontSize: 40, lineHeight: 1, marginBottom: 16 }} aria-hidden>
            {'⚠'}
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 600, margin: '0 0 8px' }}>
            This page couldn&rsquo;t load{/* i18n-ignore: renders outside next-intl */}
          </h1>
          <p style={{ fontSize: 14, color: '#94a3b8', margin: '0 0 24px' }}>
            Reload to try again, or go back.{/* i18n-ignore: renders outside next-intl */}
          </p>
          <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: '8px 16px',
                borderRadius: 8,
                border: 'none',
                background: '#e2e8f0',
                color: '#0f172a',
                fontSize: 14,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              Reload{/* i18n-ignore: renders outside next-intl */}
            </button>
            <button
              onClick={() => window.history.back()}
              style={{
                padding: '8px 16px',
                borderRadius: 8,
                border: '1px solid #334155',
                background: 'transparent',
                color: '#e2e8f0',
                fontSize: 14,
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              Back{/* i18n-ignore: renders outside next-intl */}
            </button>
          </div>
        </div>
      </body>
    </html>
  );
}
