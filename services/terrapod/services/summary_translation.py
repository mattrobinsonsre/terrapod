"""View-time translation of AI plan summaries + chat into the reader's locale (#767).

The canonical summary / failure analysis / chat thread is generated and stored
ONCE in the deployment-wide ``ai_summary.summary_language``. That stored text is
authoritative: it is what Postgres holds and what external channels (Slack, PR/MR
comments) receive. When a user opens a summary in the web UI in a *different*
locale, we translate the stored text into their locale on the fly and cache it in
Redis with a **7-day sliding TTL** — never written back to Postgres, because a
translation is non-authoritative and a summary is rarely reopened after apply.

Design invariants (agreed for #767):
  * Every locale the UI offers is a translation target — including the fun ones
    (Klingon, and the ``en-x-*`` style locales: Marklar, LOLcat, leetspeak,
    Pirate, Yoda). For those, ``target`` is a style-transform instruction the
    model applies to the prose (identifiers still stay verbatim). Only a locale
    the map doesn't recognise (an unknown ``x-privateuse`` tag) resolves to "no
    translation" — the reader then sees the canonical language.
  * Translation reuses the summariser's own model + provider auth
    (``_build_litellm_kwargs``) and debits the same daily token budget. If the
    budget is exhausted or the call fails, the caller serves the canonical text —
    translation is best-effort and MUST never break the summary view.
  * Follow-up chat prompts are normalised INTO the system language before they
    join the thread (``normalize_to_system_language``), so the stored
    ``plan_summary_messages`` thread stays monolingual and prompt-cache-friendly;
    the model answers in the system language and the UI translates replies for
    display like everything else.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import litellm
import structlog

from terrapod.config import settings

logger = structlog.get_logger(__name__)

# Sliding TTL for a cached translation. A week: long enough that an operator
# re-opening a recent run stays warm, short enough that ephemeral non-authoritative
# text doesn't accumulate. Refreshed on every read (see `_cache_get`).
_TRANSLATION_TTL_SECONDS = 7 * 24 * 60 * 60

# Every UI locale we translate to/from, keyed by the exact code AND (for real
# languages) its base, so `de-AT` → German. The value is the target descriptor
# handed to the model: a language name for real languages, or a style-transform
# instruction for the constructed / joke locales. The `en-x-*` private-use codes
# are matched exactly (they never fall back to their `en` base).
_LANGUAGE_NAMES: dict[str, str] = {
    # Real human languages.
    "en": "English",
    "en-gb": "English",
    "cy": "Welsh",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "ru": "Russian",
    "uk": "Ukrainian",
    "pl": "Polish",
    "cs": "Czech",
    "tr": "Turkish",
    "sv": "Swedish",
    "da": "Danish",
    "nb": "Norwegian",
    "no": "Norwegian",
    "fi": "Finnish",
    "pt-pt": "European Portuguese",
    "la": "Latin",
    # Right-to-left languages (#829). Write natural right-to-left prose; keep
    # code, resource addresses, and identifiers verbatim (they render LTR).
    "ar": "Arabic",
    "he": "Hebrew",
    "fa": "Persian",
    # Constructed / fun locales — a style transform of the prose, not a language.
    "tlh": "Klingon (tlhIngan Hol, the constructed language from Star Trek)",
    "en-x-marklar": (
        "Marklar-speak — English with every noun (common and proper: people, "
        'places, and things) replaced by the word "marklar", as the Marklars '
        "speak in South Park. Attributive nouns count too — a noun used to "
        'modify another noun, like "Agent" in "Agent Pools" or "VCS" in "VCS '
        'connection", is still a noun, so replace it as well ("Agent Pools" to '
        '"Marklar Marklars"). Only genuine NON-nouns stay: adjectives, verbs, '
        "articles, prepositions, conjunctions, numbers. Decide from the word's "
        'role in the sentence, not in isolation — in "Total workspaces" the '
        'word "Total" is an adjective and stays, while the noun "workspaces" '
        'becomes "marklars". Match the original word\'s capitalisation and '
        'number: singular to "marklar"/"Marklar", plural to "marklars"/'
        '"Marklars".'
    ),
    "en-x-lolcat": "LOLcat speak — the broken-English, misspelled cat-meme dialect (lolspeak)",
    "en-x-leet": (
        "leetspeak (1337 5p34k) — English with letters swapped for lookalike numerals and symbols"
    ),
    "en-x-pirate": "stereotypical Pirate English — arr, ahoy, ye, matey, and the like",
    "en-x-yoda": (
        "Yoda-speak — English reordered into object–subject–verb inversions, as "
        "Yoda from Star Wars speaks, hmm"
    ),
}


def language_name(locale: str | None) -> str | None:
    """Resolve a UI locale to a translatable English language name, or None.

    None means "not a translation target" — an unknown, private-use, or
    constructed locale. The caller serves the canonical language for those.
    """
    if not locale:
        return None
    key = locale.strip().lower()
    if key in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[key]
    # Fall back from a region/private-use tag to its base ("de-at" → "de"),
    # but never resolve an x-privateuse subtag (e.g. "en-x-leet" → "en").
    base = key.split("-", 1)[0]
    if "-x-" in key:
        return None
    return _LANGUAGE_NAMES.get(base)


def target_language(reader_locale: str | None, system_language: str | None) -> str | None:
    """The language NAME to translate INTO for this reader, or None to skip.

    Returns None (serve canonical, no model call) when the reader's locale is
    not a translatable language, or resolves to the same language the summary is
    already stored in.
    """
    target = language_name(reader_locale)
    if target is None:
        return None
    if target == language_name(system_language):
        return None
    return target


def _redis():
    from terrapod.redis.client import get_redis_client

    return get_redis_client()


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cache_key(kind: str, obj_id: str, target: str, content_hash: str) -> str:
    # `content_hash` folds the canonical text in, so regenerating a summary (new
    # text) yields a new key and the stale translation simply expires unused.
    return f"tp:ai_tr:{kind}:{obj_id}:{target.lower()}:{content_hash}"


async def _cache_get(key: str) -> str | None:
    try:
        r = _redis()
        val = await r.get(key)
        if val is not None:
            # Sliding TTL: reading refreshes the week.
            await r.expire(key, _TRANSLATION_TTL_SECONDS)
            return val.decode("utf-8") if isinstance(val, bytes) else str(val)
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.debug("translation_cache_get_failed", key=key, error=str(exc))
    return None


async def _cache_set(key: str, value: str) -> None:
    try:
        await _redis().set(key, value, ex=_TRANSLATION_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("translation_cache_set_failed", key=key, error=str(exc))


# --- The model call ----------------------------------------------------------

_TRANSLATE_SYSTEM = (
    "You are a professional technical translator for a Terraform/OpenTofu "
    "platform UI. Translate the user's content into {target}. Preserve meaning "
    "and technical accuracy exactly. Do NOT translate code, identifiers, "
    "resource addresses, provider names, HCL keywords, CLI flags, file paths, "
    "URLs, or anything inside backticks or code fences — keep those verbatim. "
    "Return ONLY the translation, with no preamble, notes, or quoting."
)

_TRANSLATE_JSON_SYSTEM = (
    "You are a professional technical translator for a Terraform/OpenTofu "
    "platform UI. You receive a JSON object. Translate ONLY the natural-language "
    "string values into {target}; keep every JSON key, structure, and any code / "
    "identifier / resource address / HCL keyword / value inside backticks "
    "verbatim. Return ONLY the same JSON object with translated string values — "
    "valid JSON, no preamble or code fence."
)


async def _translate_call(
    system_message: str, user_message: str, max_tokens: int
) -> tuple[str, int]:
    """One prose translation completion. Returns (text, output_tokens).

    Reuses the summariser's provider/auth kwargs assembly so translation speaks
    to exactly the same configured model. Raises on failure — callers catch and
    fall back to the canonical text.
    """
    from terrapod.services.summariser import _build_litellm_kwargs

    kwargs = _build_litellm_kwargs(
        kind="plan_summary",  # only selects tools, which we disable
        system_message=system_message,
        user_message=user_message,
        max_output_tokens=max_tokens,
        use_tools=False,
    )
    resp = await litellm.acompletion(**kwargs)
    if not resp.choices:
        raise RuntimeError("translation returned no choices")
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
    return text, out_tok


async def _budget_ok() -> bool:
    """False when the daily token budget is exhausted (serve canonical)."""
    from terrapod.services.summariser import _budget_remaining

    remaining = await _budget_remaining()
    return remaining is None or remaining > 0


async def _charge(tokens: int) -> None:
    from terrapod.services.summariser import _budget_charge

    await _budget_charge(tokens)


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


# --- Public API --------------------------------------------------------------


async def translate_summary(
    *,
    summary_id: str,
    description: str,
    risk_factors: list[dict[str, Any]],
    reader_locale: str | None,
) -> dict[str, Any] | None:
    """Translate a ready summary's prose into the reader's locale, or None.

    Returns ``{"description": str, "risk_factors": [...]}`` with the description
    and each factor's ``title``/``detail`` translated (``severity``/``address``
    and every other key preserved), or None when no translation applies (locale
    not a target, same language, budget exhausted, or the model call fails).
    """
    target = target_language(reader_locale, settings.ai_summary.summary_language)
    if target is None:
        return None
    if not (description or risk_factors):
        return None

    # Only the translatable natural-language fields go into the cache key + call.
    factors_min = [
        {"title": f.get("title", ""), "detail": f.get("detail", "")} for f in risk_factors
    ]
    canonical = json.dumps(
        {"description": description, "factors": factors_min}, ensure_ascii=False, sort_keys=True
    )
    key = _cache_key("summary", summary_id, target, _hash(canonical))

    cached = await _cache_get(key)
    if cached is None:
        if not await _budget_ok():
            logger.info("translation_skipped_budget", summary_id=summary_id, target=target)
            return None
        try:
            raw, out_tok = await _translate_call(
                _TRANSLATE_JSON_SYSTEM.format(target=target),
                canonical,
                max_tokens=settings.ai_summary.max_output_tokens,
            )
            await _charge(out_tok)
            cached = _strip_json_fence(raw)
            json.loads(cached)  # validate before caching
            await _cache_set(key, cached)
        except Exception as exc:  # noqa: BLE001 — never break the view
            logger.warning(
                "translation_failed", summary_id=summary_id, target=target, error=str(exc)
            )
            return None

    try:
        payload = json.loads(cached)
    except (ValueError, TypeError):
        return None

    tf = payload.get("factors", [])
    out_factors: list[dict[str, Any]] = []
    for i, orig in enumerate(risk_factors):
        merged = dict(orig)
        if i < len(tf) and isinstance(tf[i], dict):
            merged["title"] = tf[i].get("title", orig.get("title", ""))
            merged["detail"] = tf[i].get("detail", orig.get("detail", ""))
        out_factors.append(merged)
    return {
        "description": payload.get("description", description),
        "risk_factors": out_factors,
    }


async def translate_cost_summary(
    *,
    summary_id: str,
    narrative: str,
    estimated_resources: list[dict[str, Any]],
    advisories: list[dict[str, Any]],
    reader_locale: str | None,
) -> dict[str, Any] | None:
    """Translate a ready cost summary's prose into the reader's locale, or None.

    The cost analogue of :func:`translate_summary` (#871). Returns
    ``{"narrative": str, "estimated_resources": [...], "advisories": [...]}`` with
    only the natural-language fields translated — the narrative, each estimated
    resource's ``basis``, and each advisory's ``title``/``detail``. Every other
    field (``address``/``type``/``monthly``/``source`` on estimates;
    ``kind``/``monthly_saving``/``source`` on advisories) is preserved verbatim.
    Returns None when no translation applies (locale not a target, same language,
    budget exhausted, or the model call fails) — the caller then serves canonical.
    """
    target = target_language(reader_locale, settings.ai_summary.summary_language)
    if target is None:
        return None
    if not (narrative or estimated_resources or advisories):
        return None

    # Only the translatable natural-language fields go into the cache key + call.
    est_min = [{"basis": e.get("basis", "")} for e in estimated_resources]
    adv_min = [{"title": a.get("title", ""), "detail": a.get("detail", "")} for a in advisories]
    canonical = json.dumps(
        {"narrative": narrative, "estimated": est_min, "advisories": adv_min},
        ensure_ascii=False,
        sort_keys=True,
    )
    key = _cache_key("cost_summary", summary_id, target, _hash(canonical))

    cached = await _cache_get(key)
    if cached is None:
        if not await _budget_ok():
            logger.info("cost_translation_skipped_budget", summary_id=summary_id, target=target)
            return None
        try:
            raw, out_tok = await _translate_call(
                _TRANSLATE_JSON_SYSTEM.format(target=target),
                canonical,
                max_tokens=settings.ai_summary.max_output_tokens,
            )
            await _charge(out_tok)
            cached = _strip_json_fence(raw)
            json.loads(cached)  # validate before caching
            await _cache_set(key, cached)
        except Exception as exc:  # noqa: BLE001 — never break the view
            logger.warning(
                "cost_translation_failed", summary_id=summary_id, target=target, error=str(exc)
            )
            return None

    try:
        payload = json.loads(cached)
    except (ValueError, TypeError):
        return None

    te = payload.get("estimated", [])
    out_est: list[dict[str, Any]] = []
    for i, orig in enumerate(estimated_resources):
        merged = dict(orig)
        if i < len(te) and isinstance(te[i], dict):
            merged["basis"] = te[i].get("basis", orig.get("basis", ""))
        out_est.append(merged)

    ta = payload.get("advisories", [])
    out_adv: list[dict[str, Any]] = []
    for i, orig in enumerate(advisories):
        merged = dict(orig)
        if i < len(ta) and isinstance(ta[i], dict):
            merged["title"] = ta[i].get("title", orig.get("title", ""))
            merged["detail"] = ta[i].get("detail", orig.get("detail", ""))
        out_adv.append(merged)

    return {
        "narrative": payload.get("narrative", narrative),
        "estimated_resources": out_est,
        "advisories": out_adv,
    }


async def translate_architecture_critique(
    *,
    critique_id: str,
    critique: str,
    findings: list[dict[str, Any]],
    reader_locale: str | None,
) -> dict[str, Any] | None:
    """Translate a ready architecture critique's prose into the reader's locale.

    The architecture-critic analogue of :func:`translate_cost_summary`
    (#963/#1036). Returns ``{"critique": str, "findings": [...]}`` with only the
    natural-language fields translated — the critique prose and each finding's
    ``title``/``detail``. Every other field (``severity``/``category``/``address``
    on findings) is preserved verbatim. Returns None when no translation applies
    (locale not a target, same language, budget exhausted, or the model call
    fails) — the caller then serves canonical.
    """
    target = target_language(reader_locale, settings.ai_summary.summary_language)
    if target is None:
        return None
    if not (critique or findings):
        return None

    # Only the translatable natural-language fields go into the cache key + call.
    findings_min = [{"title": f.get("title", ""), "detail": f.get("detail", "")} for f in findings]
    canonical = json.dumps(
        {"critique": critique, "findings": findings_min}, ensure_ascii=False, sort_keys=True
    )
    key = _cache_key("architecture_critique", critique_id, target, _hash(canonical))

    cached = await _cache_get(key)
    if cached is None:
        if not await _budget_ok():
            logger.info(
                "critique_translation_skipped_budget", critique_id=critique_id, target=target
            )
            return None
        try:
            raw, out_tok = await _translate_call(
                _TRANSLATE_JSON_SYSTEM.format(target=target),
                canonical,
                max_tokens=settings.ai_summary.max_output_tokens,
            )
            await _charge(out_tok)
            cached = _strip_json_fence(raw)
            json.loads(cached)  # validate before caching
            await _cache_set(key, cached)
        except Exception as exc:  # noqa: BLE001 — never break the view
            logger.warning(
                "critique_translation_failed",
                critique_id=critique_id,
                target=target,
                error=str(exc),
            )
            return None

    try:
        payload = json.loads(cached)
    except (ValueError, TypeError):
        return None

    tf = payload.get("findings", [])
    out_findings: list[dict[str, Any]] = []
    for i, orig in enumerate(findings):
        merged = dict(orig)
        if i < len(tf) and isinstance(tf[i], dict):
            merged["title"] = tf[i].get("title", orig.get("title", ""))
            merged["detail"] = tf[i].get("detail", orig.get("detail", ""))
        out_findings.append(merged)
    return {
        "critique": payload.get("critique", critique),
        "findings": out_findings,
    }


async def translate_message(
    *, message_id: str, content: str, reader_locale: str | None
) -> str | None:
    """Translate one chat message's content into the reader's locale, or None."""
    target = target_language(reader_locale, settings.ai_summary.summary_language)
    if target is None or not content.strip():
        return None
    key = _cache_key("msg", message_id, target, _hash(content))
    cached = await _cache_get(key)
    if cached is not None:
        return cached
    if not await _budget_ok():
        return None
    try:
        text, out_tok = await _translate_call(
            _TRANSLATE_SYSTEM.format(target=target),
            content,
            max_tokens=settings.ai_summary.followup_max_output_tokens,
        )
        await _charge(out_tok)
        if text:
            await _cache_set(key, text)
        return text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "message_translation_failed", message_id=message_id, target=target, error=str(exc)
        )
        return None


async def normalize_to_system_language(text: str, reader_locale: str | None) -> str:
    """Translate a user follow-up prompt INTO the system language before it
    joins the thread (#767, 3b). Returns the text unchanged when the reader is
    already in the system language (or on any failure — the thread tolerates a
    stray foreign-language prompt far better than a dropped question)."""
    system = settings.ai_summary.summary_language
    reader = language_name(reader_locale)
    system_name = language_name(system)
    # Nothing to do when we can't identify a distinct real reader language.
    if reader is None or system_name is None or reader == system_name:
        return text
    if not text.strip():
        return text
    try:
        translated, out_tok = await _translate_call(
            _TRANSLATE_SYSTEM.format(target=system_name),
            text,
            max_tokens=settings.ai_summary.followup_max_output_tokens,
        )
        await _charge(out_tok)
        return translated or text
    except Exception as exc:  # noqa: BLE001
        logger.warning("prompt_normalize_failed", target=system_name, error=str(exc))
        return text
