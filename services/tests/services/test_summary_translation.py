"""Unit tests for view-time AI-summary translation (#767).

Covers the locale-resolution rules, the translate-or-skip decision, the Redis
sliding-cache path, budget gating, and follow-up prompt normalisation — all with
the LLM call + Redis mocked (services-unit tier).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import summary_translation as st

# ── locale resolution ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("en", "English"),
        ("en-GB", "English"),
        ("de", "German"),
        ("de-AT", "German"),  # region tag falls back to base
        ("cy", "Welsh"),
        ("la", "Latin"),
    ],
)
def test_language_name_real(locale, expected):
    assert st.language_name(locale) == expected


@pytest.mark.parametrize(
    "locale", ["tlh", "en-x-marklar", "en-x-lolcat", "en-x-leet", "en-x-pirate", "en-x-yoda"]
)
def test_language_name_fun_locales_resolve(locale):
    # The joke locales are valid translation targets (a style transform).
    assert st.language_name(locale) is not None


@pytest.mark.parametrize("locale", ["zz", "en-x-unknownjoke", "", None])
def test_language_name_unknown_is_none(locale):
    assert st.language_name(locale) is None


@pytest.mark.parametrize(
    "reader,system,should_translate",
    [
        ("de", "en", True),  # real, different → translate
        ("en", "en", False),  # same language → skip
        ("en-GB", "en", False),  # both English → skip
        ("de", "de", False),  # reader == system → skip
        ("fr", "de", True),  # different reals → translate
        ("en-x-leet", "en", True),  # joke locale IS a target now
        ("tlh", "en", True),  # Klingon → translate
        ("en-x-unknownjoke", "en", False),  # unrecognised → skip
        (None, "en", False),
    ],
)
def test_target_language(reader, system, should_translate):
    with patch.object(st.settings.ai_summary, "summary_language", system):
        result = st.target_language(reader, system)
    assert (result is not None) == should_translate


# ── translate_summary ───────────────────────────────────────────────────────


@pytest.fixture
def _no_cache():
    """Redis returns miss on get, accepts set; budget unlimited."""
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.expire = AsyncMock()
    with (
        patch.object(st, "_redis", return_value=r),
        patch.object(st, "_budget_ok", AsyncMock(return_value=True)),
        patch.object(st, "_charge", AsyncMock()),
    ):
        yield r


async def test_translate_summary_skips_untranslatable_locale(_no_cache):
    with patch.object(st.settings.ai_summary, "summary_language", "en"):
        out = await st.translate_summary(
            summary_id="s1", description="hi", risk_factors=[], reader_locale="en-x-leet"
        )
    assert out is None


async def test_translate_summary_translates_and_preserves_structure(_no_cache):
    translated_json = json.dumps(
        {
            "description": "Beschreibung auf Deutsch",
            "factors": [{"title": "Titel", "detail": "Detail"}],
        }
    )
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(return_value=(translated_json, 42))),
    ):
        out = await st.translate_summary(
            summary_id="s1",
            description="English description",
            risk_factors=[
                {"severity": "high", "title": "T", "detail": "D", "address": "aws_db.main"}
            ],
            reader_locale="de",
        )
    assert out["description"] == "Beschreibung auf Deutsch"
    f = out["risk_factors"][0]
    assert f["title"] == "Titel" and f["detail"] == "Detail"
    # severity + address (and any non-prose keys) are preserved untouched.
    assert f["severity"] == "high"
    assert f["address"] == "aws_db.main"


async def test_translate_summary_cache_hit_skips_model():
    r = AsyncMock()
    cached = json.dumps({"description": "cached DE", "factors": []})
    r.get = AsyncMock(return_value=cached.encode())
    r.expire = AsyncMock()
    call = AsyncMock()
    with (
        patch.object(st, "_redis", return_value=r),
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", call),
    ):
        out = await st.translate_summary(
            summary_id="s1", description="x", risk_factors=[], reader_locale="de"
        )
    assert out["description"] == "cached DE"
    call.assert_not_called()  # served from cache
    r.expire.assert_awaited()  # sliding TTL refreshed on read


async def test_translate_summary_budget_exhausted_serves_canonical(_no_cache):
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_budget_ok", AsyncMock(return_value=False)),
        patch.object(st, "_translate_call", AsyncMock()) as call,
    ):
        out = await st.translate_summary(
            summary_id="s1", description="x", risk_factors=[], reader_locale="de"
        )
    assert out is None
    call.assert_not_called()


async def test_translate_summary_model_failure_falls_back(_no_cache):
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        out = await st.translate_summary(
            summary_id="s1", description="x", risk_factors=[], reader_locale="de"
        )
    assert out is None  # caller serves canonical text


# ── normalize_to_system_language (3b helper) ─────────────────────────────────


async def test_normalize_noop_when_reader_is_system_language():
    call = AsyncMock()
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", call),
    ):
        out = await st.normalize_to_system_language("hello", reader_locale="en")
    assert out == "hello"
    call.assert_not_called()


async def test_normalize_translates_foreign_prompt_into_system_language():
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(
            st, "_translate_call", AsyncMock(return_value=("Why is the DB replaced?", 10))
        ),
        patch.object(st, "_charge", AsyncMock()),
    ):
        out = await st.normalize_to_system_language(
            "Warum wird die DB ersetzt?", reader_locale="de"
        )
    assert out == "Why is the DB replaced?"


async def test_normalize_falls_back_to_original_on_failure():
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(st, "_charge", AsyncMock()),
    ):
        out = await st.normalize_to_system_language("Frage", reader_locale="de")
    assert out == "Frage"  # a stray foreign prompt beats a dropped question
