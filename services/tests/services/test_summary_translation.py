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


# ── translate_cost_summary (#871) ────────────────────────────────────────────


async def test_translate_cost_summary_skips_untranslatable_locale(_no_cache):
    with patch.object(st.settings.ai_summary, "summary_language", "en"):
        out = await st.translate_cost_summary(
            summary_id="c1",
            narrative="hi",
            estimated_resources=[],
            advisories=[],
            reader_locale="en-x-leet",
        )
    assert out is None


async def test_translate_cost_summary_translates_prose_preserves_data(_no_cache):
    # Only narrative + estimate.basis + advisory.title/detail are translated;
    # address/type/monthly/source + kind/monthly_saving/source stay verbatim.
    translated_json = json.dumps(
        {
            "narrative": "Zusammenfassung auf Deutsch",
            "estimated": [{"basis": "Grundlage DE"}],
            "advisories": [{"title": "Titel DE", "detail": "Detail DE"}],
        }
    )
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(return_value=(translated_json, 42))),
    ):
        out = await st.translate_cost_summary(
            summary_id="c1",
            narrative="English narrative",
            estimated_resources=[
                {
                    "address": "azurerm_storage_account.a",
                    "type": "azurerm_storage_account",
                    "monthly": {"min": 5.0, "max": 8.0},
                    "basis": "LRS hot ~100GB",
                    "source": "ai-estimate",
                }
            ],
            advisories=[
                {
                    "kind": "reserved",
                    "title": "T",
                    "detail": "D",
                    "monthly_saving": {"min": 10.0, "max": 20.0},
                    "source": "ai-estimate",
                }
            ],
            reader_locale="de",
        )
    assert out["narrative"] == "Zusammenfassung auf Deutsch"
    e = out["estimated_resources"][0]
    assert e["basis"] == "Grundlage DE"
    # non-prose fields preserved untouched
    assert e["address"] == "azurerm_storage_account.a"
    assert e["monthly"] == {"min": 5.0, "max": 8.0}
    assert e["source"] == "ai-estimate"
    a = out["advisories"][0]
    assert a["title"] == "Titel DE" and a["detail"] == "Detail DE"
    assert a["kind"] == "reserved"
    assert a["monthly_saving"] == {"min": 10.0, "max": 20.0}
    assert a["source"] == "ai-estimate"


async def test_translate_cost_summary_empty_is_none(_no_cache):
    with patch.object(st.settings.ai_summary, "summary_language", "en"):
        out = await st.translate_cost_summary(
            summary_id="c1", narrative="", estimated_resources=[], advisories=[], reader_locale="de"
        )
    assert out is None


async def test_translate_cost_summary_model_failure_falls_back(_no_cache):
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        out = await st.translate_cost_summary(
            summary_id="c1",
            narrative="x",
            estimated_resources=[],
            advisories=[{"kind": "other", "title": "t", "detail": "d", "monthly_saving": None}],
            reader_locale="de",
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


# ── translate_architecture_critique (#1036) ──────────────────────────────────


async def test_translate_architecture_skips_untranslatable_locale(_no_cache):
    with patch.object(st.settings.ai_summary, "summary_language", "en"):
        out = await st.translate_architecture_critique(
            critique_id="a1",
            architecture={"summary": "hi"},
            findings=[],
            deferred=[],
            reader_locale="en-x-leet",
        )
    assert out is None


async def test_translate_architecture_empty_is_none(_no_cache):
    with patch.object(st.settings.ai_summary, "summary_language", "en"):
        out = await st.translate_architecture_critique(
            critique_id="a1", architecture={}, findings=[], deferred=[], reader_locale="de"
        )
    assert out is None


async def test_translate_architecture_translates_prose_preserves_code_fields(_no_cache):
    # summary/tiers/data_stores/blast_radius + finding title/detail/recommendation
    # + deferred are translated; severity/category/resource_address/grounded_in stay.
    translated_json = json.dumps(
        {
            "architecture": {
                "summary": "Zusammenfassung DE",
                "tiers": ["Netz-Tier"],
                "data_stores": ["DB"],
                "blast_radius": "Radius DE",
            },
            "findings": [
                {"title": "Titel DE", "detail": "Detail DE", "recommendation": "Empfehlung DE"}
            ],
            "deferred": ["Zurückgestellt DE"],
        }
    )
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(return_value=(translated_json, 42))),
    ):
        out = await st.translate_architecture_critique(
            critique_id="a1",
            architecture={
                "summary": "EN summary",
                "tiers": ["net"],
                "data_stores": ["db"],
                "blast_radius": "R",
            },
            findings=[
                {
                    "severity": "high",
                    "category": "reliability",
                    "title": "T",
                    "detail": "D",
                    "recommendation": "Rec",
                    "resource_address": "aws_db_instance.main",
                    "grounded_in": "security-scan",
                }
            ],
            deferred=["deferred EN"],
            reader_locale="de",
        )
    assert out["architecture"]["summary"] == "Zusammenfassung DE"
    assert out["architecture"]["blast_radius"] == "Radius DE"
    f = out["findings"][0]
    assert f["title"] == "Titel DE" and f["detail"] == "Detail DE"
    assert f["recommendation"] == "Empfehlung DE"
    # code-shaped fields preserved verbatim
    assert f["severity"] == "high"
    assert f["category"] == "reliability"
    assert f["resource_address"] == "aws_db_instance.main"
    assert f["grounded_in"] == "security-scan"
    assert out["deferred"] == ["Zurückgestellt DE"]


async def test_translate_architecture_budget_exhausted_serves_canonical(_no_cache):
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_budget_ok", AsyncMock(return_value=False)),
        patch.object(st, "_translate_call", AsyncMock()) as call,
    ):
        out = await st.translate_architecture_critique(
            critique_id="a1",
            architecture={"summary": "x"},
            findings=[],
            deferred=[],
            reader_locale="de",
        )
    assert out is None
    call.assert_not_called()


async def test_translate_architecture_model_failure_falls_back(_no_cache):
    with (
        patch.object(st.settings.ai_summary, "summary_language", "en"),
        patch.object(st, "_translate_call", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        out = await st.translate_architecture_critique(
            critique_id="a1",
            architecture={"summary": "x"},
            findings=[],
            deferred=[],
            reader_locale="de",
        )
    assert out is None
