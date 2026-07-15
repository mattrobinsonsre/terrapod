// i18n novelty-locale harness (#835 follow-up).
//
// The novelty/gimmick locales (en-x-marklar, en-x-lolcat, en-x-pirate,
// en-x-yoda) are word-substitution jokes over the English source. They can't
// be produced by a naive script — deciding, say, which words are nouns for the
// "marklar" gag needs sentence context ("Total" in "Total [workspaces]" is an
// adjective, not a noun). So the transform step is an AI fill: this harness
// does only the mechanical, deterministic scaffolding around it —
//
//   extract <locale>  → dedupe en.json's leaf strings into numbered chunk files
//                       under _work/<locale>/in-NN.json ({ "id": "english" }),
//                       small enough for one AI agent to transform faithfully.
//   merge <locale>    → read _work/<locale>/out-NN.json ({ "id": "transformed" }),
//                       rebuild messages/<locale>.json from en.json's structure
//                       (so keys can NEVER drift), and validate every string:
//                       same ICU argument names + same rich-text tag names as
//                       the source, or the string is REJECTED (falls back to
//                       English + logged). This guarantees the completeness +
//                       ICU gates (npm run i18n:check) stay green by construction.
//
// en-x-leet is a pure character substitution that was already applied
// consistently (only symbol/number-only strings stay English), so it is left
// as-is and is NOT regenerated here — this harness only handles the
// word-substitution locales that need the context-aware AI fill.
//
// Usage:
//   node harness.mjs extract marklar
//   node harness.mjs merge   marklar

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MESSAGES = path.resolve(HERE, '..', '..', 'messages');
const WORK = path.join(HERE, '_work');
const CHUNK = 190; // strings per AI chunk — small enough to transform carefully

const EN = JSON.parse(fs.readFileSync(path.join(MESSAGES, 'en.json'), 'utf8'));

// ---- flatten / rebuild helpers (structure is authoritative from en.json) ----
function flatten(obj, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object') flatten(v, key, out);
    else out[key] = v;
  }
  return out;
}

// Rebuild a nested catalog with the SAME shape as en.json, taking each leaf
// value from `resolve(key, englishValue)`.
function rebuild(obj, resolve, prefix = '') {
  const out = Array.isArray(obj) ? [] : {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object') out[k] = rebuild(v, resolve, key);
    else out[k] = resolve(key, v);
  }
  return out;
}

// ---- ICU / tag signature: what must be preserved verbatim ----
// The real ICU *arguments* — {name}, the `count` in {count, plural, …}, nested
// placeholders inside branches — plus rich-text tag names. Order-independent
// multiset. Crucially this must NOT count a plural/select BRANCH BODY (the
// translatable text in `one {workspace}` / `other {workspaces}`) as an argument:
// that text is exactly what the joke transform changes, so counting it would
// reject every translated plural string as a "dropped arg".
function signature(s) {
  if (typeof s !== 'string') return { args: [], tags: [] };
  const tags = [...s.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9]*)/g)].map((m) => m[1]).sort();
  const args = [];
  const re = /\{\s*([a-zA-Z0-9_]+)\s*([},])/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    // `{name,` always introduces a real argument (plural/select/number/date…).
    if (m[2] === ',') { args.push(m[1]); continue; }
    // A bare `{name}` is a real placeholder UNLESS it's a plural/select branch
    // body — i.e. immediately preceded by a selector keyword or `=N`.
    const before = s.slice(0, m.index);
    if (/(?:^|[\s{])(?:zero|one|two|few|many|other|=\d+)\s*$/.test(before)) continue;
    args.push(m[1]);
  }
  return { args: args.sort(), tags };
}
function sameSig(a, b) {
  const sa = signature(a), sb = signature(b);
  return JSON.stringify(sa) === JSON.stringify(sb);
}

// ---- unique English leaf strings (the real work unit) ----
function uniqueStrings() {
  const flat = flatten(EN);
  const seen = new Map(); // english -> id
  const list = [];        // { id, en }
  for (const v of Object.values(flat)) {
    if (typeof v !== 'string') continue;
    if (seen.has(v)) continue;
    const id = String(list.length);
    seen.set(v, id);
    list.push({ id, en: v });
  }
  return { list, byEnglish: seen };
}

// ---------------------------------------------------------------------------
const [, , cmd, locArg] = process.argv;

function chunkDir(loc) { return path.join(WORK, loc); }

if (cmd === 'extract') {
  const loc = locArg;
  if (!loc) throw new Error('usage: harness.mjs extract <locale-suffix>');
  const dir = chunkDir(loc);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const { list } = uniqueStrings();
  let n = 0;
  for (let i = 0; i < list.length; i += CHUNK) {
    const slice = list.slice(i, i + CHUNK);
    const obj = {};
    for (const { id, en } of slice) obj[id] = en;
    const name = `in-${String(n).padStart(2, '0')}.json`;
    fs.writeFileSync(path.join(dir, name), JSON.stringify(obj, null, 2));
    n++;
  }
  console.log(`extracted ${list.length} unique strings into ${n} chunk(s) at ${dir}`);
  console.log(`chunks: ${Array.from({ length: n }, (_, i) => `in-${String(i).padStart(2, '0')}`).join(' ')}`);
} else if (cmd === 'merge') {
  const loc = locArg;
  if (!loc) throw new Error('usage: harness.mjs merge <locale-suffix>');
  const dir = chunkDir(loc);
  // Gather every out-NN.json into one id -> transformed map.
  const map = {};
  for (const f of fs.readdirSync(dir).filter((f) => /^out-\d+\.json$/.test(f))) {
    Object.assign(map, JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')));
  }
  // Key-level overrides: context-corrections that the bare-string AI fill can't
  // make because dedup strips the key (e.g. the "Total/Health/Locked" stat tiles
  // are elided-noun modifiers — "[Health] workspaces" — not standalone nouns, so
  // they stay English even though "Health" in isolation is a noun). Applied by
  // KEY (precise), before the AI map, so they persist across re-runs.
  const overridesPath = path.join(HERE, `${loc}-overrides.json`);
  const overrides = fs.existsSync(overridesPath)
    ? JSON.parse(fs.readFileSync(overridesPath, 'utf8'))
    : {};
  const { byEnglish } = uniqueStrings();
  let ok = 0, fellBack = 0, missing = 0, overridden = 0;
  const badSig = [];
  const resolve = (key, en) => {
    if (typeof en !== 'string') return en;
    if (Object.prototype.hasOwnProperty.call(overrides, key)) { overridden++; return overrides[key]; }
    const id = byEnglish.get(en);
    const cand = id != null ? map[id] : undefined;
    if (cand == null) { missing++; return en; }
    if (!sameSig(en, cand)) { fellBack++; if (badSig.length < 20) badSig.push(`${JSON.stringify(en)} -> ${JSON.stringify(cand)}`); return en; }
    ok++;
    return cand;
  };
  const full = `en-x-${loc}`;
  const rebuilt = rebuild(EN, resolve);
  fs.writeFileSync(path.join(MESSAGES, `${full}.json`), JSON.stringify(rebuilt, null, 2) + '\n');
  console.log(`merged ${full}: ok=${ok} fellBackOnBadICU=${fellBack} missingFromAI=${missing}`);
  if (badSig.length) { console.log('--- ICU/tag mismatches (kept English):'); console.log(badSig.join('\n')); }
} else {
  console.log('usage: harness.mjs <extract|merge> [locale-suffix]');
  process.exit(1);
}
