# Internationalization (i18n)

The Terrapod web UI is fully internationalized with
[next-intl](https://next-intl.dev/). Every user-facing string is translated,
and the AI plan-summary / chat is translated at view time by the model.

## Offered languages

Terrapod ships **24 real languages** plus a handful of novelty locales. The
authoritative list is `locales` in `web/src/i18n/config.ts`; the message
catalogs live in `web/messages/<code>.json`.

| | Languages |
|---|---|
| **European** | English (`en`), English UK (`en-GB`), German (`de`), French (`fr`), Spanish (`es`), Italian (`it`), Dutch (`nl`), Portuguese — Brazil (`pt-BR`) and Portugal (`pt-PT`), Russian (`ru`), Ukrainian (`uk`), Polish (`pl`), Czech (`cs`), Swedish (`sv`), Danish (`da`), Norwegian (`nb`), Finnish (`fi`), Welsh (`cy`), Latin (`la`) |
| **Asian** | Japanese (`ja`), Korean (`ko`), Chinese — Simplified (`zh-CN`) and Traditional (`zh-TW`), Turkish (`tr`) |
| **Novelty** | Klingon (`tlh`), Marklar, LOLcat, leetspeak, Pirate, Yoda (`en-x-*`) |

Right-to-left languages (Arabic, Hebrew, Persian) are tracked separately in
issue #829 — they need `dir="rtl"` + mirrored layout, not just a catalog.

## How the locale is chosen

Resolved per-request, with no `/<locale>/` URL segment:

1. The `NEXT_LOCALE` cookie (set by the globe switcher in the nav).
2. Otherwise the browser's `Accept-Language` header.
3. Otherwise `en`.

The switcher writes the cookie and refreshes; the server layout re-runs
`src/i18n/request.ts` and re-provides messages — no reload, no URL change.

`en` (US English) is the **source** catalog. Every other locale deep-merges
over `en`, so a missing key can never render `MISSING_KEY` — but see the
completeness gate below: a *partial* locale is never actually offered.

## AI plan-summary translation

The AI plan summary, failure analysis, and chat are prose, so they are
translated on a different axis from the UI chrome:

- The canonical summary is **generated once** in the deployment-wide
  `ai.summary_language` (default `en`). That copy is what Postgres stores and
  what ships to Slack / PR comments (no viewer there to translate for).
- In the web UI it is **translated on view** into the reader's locale and
  cached per-locale in Redis (7-day sliding TTL, never persisted).
- Follow-up chat prompts are normalised into the system language before they
  join the thread, so the stored thread stays monolingual and
  prompt-cache-friendly.

Resource addresses, HCL, and code identifiers stay verbatim in every language.

## Two CI gates keep it honest

Both run in the **Frontend Lint** CI job:

1. **Completeness** (`npm run i18n:check`) — every *offered* locale must be
   100% key-parity with `en` **and** ICU-valid, or it isn't offered. There is
   no "partial, falls back to English" middle ground. `en-GB` is the one
   exception: a dialect override carrying only the spelling deltas, checked as
   a subset.
2. **No hardcoded strings** (`npm run i18n:lint`) — an AST guard that fails
   when a new English literal appears in JSX (text nodes or user-facing
   attributes) instead of going through `t(...)`. It ratchets against a
   committed baseline (`web/scripts/i18n-hardcoded-allowlist.json`); genuine
   non-copy (code identifiers, product names) is suppressed with an
   `i18n-ignore` comment on the line.

## Adding a language

1. Add the locale code to `locales` in `web/src/i18n/config.ts`, plus a native
   name in `localeNames` and a short chip in
   `web/src/components/locale-switcher.tsx`.
2. Create `web/messages/<code>.json` — a **complete** translation of every key
   in `web/messages/en.json`. Preserve ICU placeholders (`{name}`, plurals with
   the language's correct CLDR categories), rich-text tags (`<code>`,
   `<strong>`, …), and leave code identifiers / product names verbatim.
3. Add the language name to `_LANGUAGE_NAMES` in
   `services/terrapod/services/summary_translation.py` so AI summaries
   translate into it too.
4. `npm run i18n:check` must pass (100% parity) and `npm run build` must
   succeed. A locale that isn't complete must not be added to `locales`.
