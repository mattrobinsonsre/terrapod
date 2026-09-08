"""Phase vocabulary comes from the engine, not from literals (#1407 §11, #1521).

Internal state names never change — a run is `planning` whatever engine it
belongs to. What a *person* is shown does: Terraform plans and applies, Pulumi
previews and updates, Ansible checks and runs. #1407 §3 requires that difference
stay visible in the API and the UI "not smoothed over", and a hardcoded
"Running terraform plan" is precisely the smoothing-over it forbids.

The guard is here rather than in the frontend suite because it is a *structural*
invariant of the message catalogues and the strategy, and because with Terraform
the only engine every rendering assertion passes either way — only the source can
be checked.
"""

from __future__ import annotations

import json
import pathlib

import pytest


def _messages_dir() -> pathlib.Path:
    """Locate the catalogues from either layout.

    Locally the repo root is `parents[3]`; inside the test image the tree is
    flattened to `/app`, making it `parents[2]`. Searching works in both rather
    than hard-coding one and silently skipping in the other — a test that only
    runs on a laptop is not a guard.
    """
    here = pathlib.Path(__file__).resolve()
    for base in here.parents[1:6]:
        candidate = base / "web" / "messages"
        if candidate.is_dir():
            return candidate
    raise AssertionError(
        "web/messages was not found from either layout — if the test image "
        "stopped copying it, this guard went quiet rather than red"
    )


MESSAGES = _messages_dir()

#: The phase states whose display words belong to the engine.
PHASE_STATES = ("planning", "planned", "applying", "applied")

#: The one locale that is a spelling-delta override rather than a full catalogue,
#: and so legitimately carries only what differs from `en`.
SUBSET_LOCALE = "en-GB"


def _catalogues() -> list[pathlib.Path]:
    return sorted(f for f in MESSAGES.glob("*.json") if f.stem != SUBSET_LOCALE)


def test_every_offered_locale_carries_the_engine_namespace():
    """A locale is complete or it is not offered — including this namespace.

    English deep-merges under a partial locale at runtime, so a missing block
    renders in English rather than failing. That is a crash guard, not a licence
    to ship a half-translated language.
    """
    missing = []
    for f in _catalogues():
        phases = json.loads(f.read_text()).get("phases", {}).get("terraform")
        if not phases:
            missing.append(f.stem)
    assert not missing, f"locales without phases.terraform: {missing}"


@pytest.mark.parametrize("group", ["runStatus", "status", "activity"])
def test_the_namespace_is_populated_in_every_locale(group: str):
    incomplete = []
    for f in _catalogues():
        block = json.loads(f.read_text()).get("phases", {}).get("terraform", {}).get(group, {})
        if not block:
            incomplete.append(f.stem)
    assert not incomplete, f"locales missing phases.terraform.{group}: {incomplete}"


def test_no_locale_left_a_phase_word_in_english():
    """The restructure moved existing translations; it must not have dropped any.

    Copying a block between namespaces is exactly the operation that silently
    leaves English behind, and the joke locales are where that shows up least —
    nobody reads `tlh` closely enough to notice.
    """
    en = json.loads((MESSAGES / "en.json").read_text())["phases"]["terraform"]
    english_activity = en["activity"]["planning"]

    # Locales whose phase wording legitimately matches English: none of the
    # offered set, since even the dialect locales transform this string.
    same_as_english = []
    for f in _catalogues():
        if f.stem == "en":
            continue
        got = json.loads(f.read_text())["phases"]["terraform"]["activity"]["planning"]
        if got == english_activity:
            same_as_english.append(f.stem)

    assert not same_as_english, (
        "these locales carry the English phase string verbatim, which means the "
        f"translation was lost rather than moved: {same_as_english}"
    )


def test_the_strategy_declares_a_vocabulary_namespace():
    """The engine picks a key namespace, never a string.

    Holding English on the strategy would put user-facing words behind the
    engine gate and outside the translation pipeline, which is how a second
    engine ends up shipping untranslatable labels.
    """
    from terrapod.engines import strategy_for

    strategy = strategy_for("terraform")
    assert strategy.vocabulary == "terraform"
    assert not any(c.isspace() for c in strategy.vocabulary), (
        "the vocabulary is a message-key segment, so it cannot contain whitespace"
    )


def test_the_declared_namespace_exists_in_the_catalogues():
    """A strategy naming a namespace nothing carries renders raw keys.

    This is the join between the two halves — the Python side choosing a name and
    the catalogues providing it — and nothing else checks that they agree.
    """
    from terrapod.engines import known_engines, strategy_for

    en = json.loads((MESSAGES / "en.json").read_text()).get("phases", {})
    for engine in known_engines():
        namespace = strategy_for(engine).vocabulary
        assert namespace in en, (
            f"{engine} declares vocabulary {namespace!r} but web/messages/en.json "
            f"has no phases.{namespace} block — the UI would render raw keys"
        )


def test_the_web_knows_every_engine_the_api_serves():
    """The frontend's fallback set must not drift from the registry.

    `phase-vocabulary.ts` keeps a local set of engines whose words the
    catalogues carry, and falls back to Terraform's for anything else. That
    fallback is deliberate — a missing translation should look like slightly
    wrong wording rather than a broken page — but it also means adding an engine
    and forgetting the frontend produces *plausible* output: a Pulumi run
    labelled "planning". Nothing else would catch that, because it renders fine.
    """
    from terrapod.engines import known_engines, strategy_for

    src = MESSAGES.parent / "src/lib/phase-vocabulary.ts"
    assert src.is_file(), (
        f"{src} was not found — skipping here instead would leave this guard "
        "green while asserting nothing, which is worse than not having it"
    )

    text = src.read_text()
    for engine in known_engines():
        namespace = strategy_for(engine).vocabulary
        assert f"'{namespace}'" in text or f'"{namespace}"' in text, (
            f"the API serves engine {engine!r} (vocabulary {namespace!r}) but "
            "web/src/lib/phase-vocabulary.ts does not know it — its runs would "
            "silently render with Terraform's words"
        )
