"""AI plan summariser + run-failure analyser (#401, #419).

When enabled via ``ai_summary.enabled``, the API triggers an
asynchronous call after every terminal run transition:
  - ``planned`` → ``kind=plan_summary``: describe changes + rate risk.
  - ``errored`` at any point → ``kind=failure_analysis``: explain why
    the run failed and suggest fixes. Plan-phase errors read the
    plan log; apply-phase errors read the apply log and the prompt
    grows apply-specific guidance (identify the failed resource,
    identify resources that completed before the failure, call out
    partial-state, rank fixes by recovery type). Workspaces flagged
    ``state_diverged`` also surface that in a dedicated prompt block.

The model call goes through the LiteLLM Python library
(``litellm.acompletion``). Terrapod always speaks the OpenAI Chat
Completions request shape; LiteLLM handles the per-provider request
translation, auth, and response normalisation in-process. No gateway
deployment, no second pod.

The model string's prefix selects the provider:

  - ``bedrock/<model-id>`` — AWS Bedrock via boto3. IAM auth via the
    pod's IRSA service account; optional ``sts:AssumeRole`` hop into a
    cross-account role when ``auth.aws_role_arn`` is set. Works for
    Anthropic Claude, Amazon Nova, Mistral, Meta Llama, etc.
  - ``openai/<model-id>`` — OpenAI direct, with ``auth.api_key``.
  - ``anthropic/<model-id>`` — Anthropic direct, with ``auth.api_key``.
  - ``gemini/<model-id>`` — Google AI Studio, with ``auth.api_key``.
  - ``azure/<deployment-name>`` — Azure OpenAI, with ``api_base`` + key.
  - Self-hosted OpenAI-compat (vLLM, deployed LiteLLM proxy, etc.) —
    use ``openai/<model>`` with ``api_base`` pointing at the endpoint.

The model's JSON response is parsed and stored in the
``plan_summaries`` table; an SSE ``plan_summary_ready`` event is
emitted on the per-workspace channel so the UI re-fetches.

Daily token budget (``ai_summary.daily_token_budget``) is tracked in a
Redis counter per UTC day. Once exhausted, further calls are skipped
(``status='skipped'``) without raising — run lifecycle is never
affected by this feature.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import pathlib
import subprocess
import tarfile
import tempfile
import uuid

import litellm
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import PlanSummary, PlanSummaryMessage, Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.summariser_prompt import render_prompt, tool_for_kind
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    plan_json_output_key,
    plan_log_key,
)

logger = get_logger(__name__)


# --- Input retrieval ---------------------------------------------------------


def _truncate_tail(data: bytes, max_bytes: int) -> str:
    """Return ``data`` decoded as UTF-8 (best-effort), tail-trimmed.

    Logs are tail-relevant — the failure is at the end. We drop the head
    when over budget rather than the tail.
    """
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    trimmed = data[-max_bytes:]
    return f"[... {len(data) - max_bytes} bytes truncated from head ...]\n" + trimmed.decode(
        "utf-8", errors="replace"
    )


def _truncate_head(data: bytes, max_bytes: int) -> str:
    """Return ``data`` decoded as UTF-8, truncated from the tail.

    Plan JSON's structural value is at the head — provider config and
    resource_changes come first.
    """
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")
    return (
        data[:max_bytes].decode("utf-8", errors="replace")
        + f"\n[... {len(data) - max_bytes} bytes truncated from tail ...]"
    )


def _extract_tf_sources(tarball: bytes, max_bytes: int) -> str:
    """Read .tf files from a config-version tarball, concatenated.

    Files are read in tar order until the byte cap is hit. Each file is
    prefixed with a header so the model can attribute snippets to paths.
    Defends against zip-bombs by bailing as soon as the cap is reached.
    """
    if max_bytes <= 0:
        return ""

    buf = io.StringIO()
    written = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".tf"):
                    continue
                if member.size > max_bytes - written:
                    break
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                content = fobj.read().decode("utf-8", errors="replace")
                header = f"\n# === {member.name} ===\n"
                buf.write(header)
                buf.write(content)
                written += len(header) + len(content)
                if written >= max_bytes:
                    break
    except tarfile.TarError as e:
        logger.warning("Failed to read CV tarball for AI context", error=str(e))
        return ""
    return buf.getvalue()


def _clean_plan_json_bytes(raw: bytes) -> bytes:
    """Strip definitionally-uninformative noise from terraform plan JSON.

    The model was observed confabulating "upgrade" narratives from
    `before` / `after` snapshot fields on no-op resources. The fix is
    twofold: prompt the model to trust `change.actions` (done in the
    skill prompt), and remove the snapshot noise so it physically
    cannot be hallucinated.

    Drops:
      • resource_changes entries where every action is in
        {no-op, read} AND change.importing is unset
      • output_changes entries with actions == ["no-op"]
      • the top-level "prior_state" key (full pre-refresh state
        snapshot, redundant with resource_changes.before)

    Partitions resource_drift into two top-level keys:
      • resource_drift — entries whose address ALSO has a real
        resource_changes entry. These are drift the apply IS
        reverting; the model treats them as elevated risk.
      • drift_observed_no_apply_action — entries whose address has
        no corresponding resource_changes (terraform refreshed and
        reconciled, apply does nothing). The change.actions array
        is rewritten to ["drift_observed"] so the model cannot
        pattern-match destroy/update framing onto them — that
        conflation produced false destroy summaries in practice.

    Preserves:
      • read-only entries when paired with import (rare but valid)

    On any structural anomaly (non-dict plan, malformed entries) returns
    the input unchanged. Cleaner is best-effort: never fail the call.
    """
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(plan, dict):
        return raw

    plan.pop("prior_state", None)

    rcs = plan.get("resource_changes")
    if isinstance(rcs, list):
        kept_rcs = [r for r in rcs if _resource_change_is_informative(r)]
        plan["resource_changes"] = kept_rcs

    _partition_resource_drift(plan)

    ocs = plan.get("output_changes")
    if isinstance(ocs, dict):
        plan["output_changes"] = {
            k: v
            for k, v in ocs.items()
            if not (isinstance(v, dict) and v.get("actions") == ["no-op"])
        }

    return json.dumps(plan, separators=(",", ":")).encode("utf-8")


def _partition_resource_drift(plan: dict[str, object]) -> None:
    """Split `resource_drift` based on whether each address has a real change.

    Mutates `plan` in place. See `_clean_plan_json_bytes` for the
    rationale. Must be called AFTER `resource_changes` no-ops have been
    pruned so the address lookup is over the informative set only.
    """
    drift = plan.get("resource_drift")
    if not isinstance(drift, list) or not drift:
        return

    rcs = plan.get("resource_changes")
    rc_addresses: set[object] = set()
    if isinstance(rcs, list):
        for r in rcs:
            if isinstance(r, dict):
                addr = r.get("address")
                if addr is not None:
                    rc_addresses.add(addr)

    reverted: list[object] = []
    observed: list[object] = []
    for d in drift:
        if not isinstance(d, dict):
            reverted.append(d)
            continue
        addr = d.get("address")
        if addr in rc_addresses:
            reverted.append(d)
            continue
        observed.append(_neutralize_drift_actions(d))

    plan["resource_drift"] = reverted
    if observed:
        plan["drift_observed_no_apply_action"] = observed


def _neutralize_drift_actions(entry: dict[str, object]) -> dict[str, object]:
    """Replace ["delete"]/["update"] action labels with ["drift_observed"].

    The replacement is intentionally non-standard — the model treats
    `create`/`update`/`delete` as planned actions, so any of those
    appearing on a drift entry is a fertile source of misreads. A
    label the model has never seen before forces it to consult the
    prompt's drift-handling section instead of pattern-matching.
    """
    out = dict(entry)
    change = out.get("change")
    if isinstance(change, dict):
        new_change = dict(change)
        new_change["actions"] = ["drift_observed"]
        out["change"] = new_change
    return out


def _resource_change_is_informative(r: object) -> bool:
    """Return True when this resource_change should reach the model."""
    if not isinstance(r, dict):
        return True
    change = r.get("change")
    if not isinstance(change, dict):
        return True
    if change.get("importing") is not None:
        return True
    actions = change.get("actions")
    if not isinstance(actions, list) or not actions:
        return True
    return any(a not in ("no-op", "read") for a in actions)


def _extract_tf_files_to_dir(tarball: bytes, target: pathlib.Path) -> int:
    """Extract *.tf / *.tfvars files from a config-version tarball.

    Returns the number of files written. Defends against zip-slip via
    member-name normalisation; skips entries whose path escapes target.
    """
    written = 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            if not (member.name.endswith(".tf") or member.name.endswith(".tfvars")):
                continue
            safe = pathlib.PurePosixPath(member.name).as_posix()
            if safe.startswith("/") or ".." in safe.split("/"):
                continue
            fobj = tar.extractfile(member)
            if fobj is None:
                continue
            dest = target / safe
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(fobj.read())
            written += 1
    return written


def _build_code_diff(prev_tarball: bytes | None, cur_tarball: bytes, max_bytes: int) -> str:
    """Return a unified diff of *.tf / *.tfvars between two CV tarballs.

    Returns "" when:
      • max_bytes is 0 (feature disabled)
      • prev_tarball is None (first run for this workspace, or the
        previous CV's tarball has been GC'd by artifact retention)
      • either tarball is unreadable
      • the two trees contain no .tf / .tfvars files
      • the trees are identical (empty diff)
      • git is missing or times out

    Uses `git diff --no-index` against two temp directories. Falls back
    silently on any failure: CODE_DIFF is best-effort context, never
    blocks the summariser.
    """
    if max_bytes <= 0 or prev_tarball is None:
        return ""

    with tempfile.TemporaryDirectory(prefix="tp-aisum-diff-") as tmp:
        tmp_root = pathlib.Path(tmp)
        prev_dir = tmp_root / "previous"
        cur_dir = tmp_root / "current"
        prev_dir.mkdir()
        cur_dir.mkdir()
        try:
            prev_n = _extract_tf_files_to_dir(prev_tarball, prev_dir)
            cur_n = _extract_tf_files_to_dir(cur_tarball, cur_dir)
        except tarfile.TarError as e:
            logger.debug("CODE_DIFF: tarball extract failed", error=str(e))
            return ""

        if prev_n == 0 and cur_n == 0:
            return ""

        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--unified=3",
                    "--no-color",
                    "--",
                    "previous",
                    "current",
                ],
                cwd=tmp_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug("CODE_DIFF: git diff failed", error=str(e))
            return ""

        # git diff --no-index exits 1 when there's a diff, 0 when there
        # isn't — both are success for us. Anything else (e.g. 128 from
        # git missing) is a real failure.
        if proc.returncode not in (0, 1):
            logger.debug(
                "CODE_DIFF: git diff returned unexpected exit code",
                returncode=proc.returncode,
                stderr=proc.stderr[:200],
            )
            return ""

        diff = proc.stdout
        if not diff.strip():
            return ""

        if len(diff) > max_bytes:
            diff = (
                diff[:max_bytes] + f"\n[... {len(diff) - max_bytes} bytes truncated from tail ...]"
            )
        return diff


async def _find_previously_applied_cv_id(
    db: AsyncSession, workspace_id: uuid.UUID, current_run_id: uuid.UUID
) -> uuid.UUID | None:
    """Return the configuration_version_id of the most recently *applied*
    Run on the workspace, excluding ``current_run_id``.

    None when no prior applied run exists (first run, or all prior
    runs were plan-only / errored / discarded). The summariser falls
    back to no CODE_DIFF in that case.
    """
    stmt = (
        select(Run.configuration_version_id)
        .where(
            Run.workspace_id == workspace_id,
            Run.status == "applied",
            Run.id != current_run_id,
            Run.configuration_version_id.is_not(None),
        )
        .order_by(Run.updated_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _gather_inputs(db: AsyncSession, run: Run, kind: str) -> tuple[str, str, str, str, str]:
    """Return ``(primary_input, primary_label, primary_lang, code_context, code_diff)``.

    primary_input is the (cleaned) plan JSON for ``plan_summary`` or
    the plan log for ``failure_analysis``. code_context is the
    concatenated current .tf source. code_diff is a unified diff of
    *.tf / *.tfvars between this run's CV and the previously-applied
    CV (when both tarballs are available).
    """
    storage = get_storage()
    cfg = settings.ai_summary

    if kind == "plan_summary":
        key = plan_json_output_key(str(run.workspace_id), str(run.id))
        primary_label = "PLAN_JSON"
        primary_lang = "json"
        try:
            raw = await storage.get(key)
        except Exception as e:
            logger.warning(
                "plan JSON not available for summariser", run_id=str(run.id), error=str(e)
            )
            return "", primary_label, primary_lang, "", ""
        # Clean BEFORE truncation so the head-truncate budget is spent
        # on actual changes, not no-op snapshot noise.
        cleaned = await asyncio.to_thread(_clean_plan_json_bytes, raw)
        primary = _truncate_head(cleaned, cfg.plan_json_max_bytes)
    else:
        # failure_analysis. Choose log key by phase: apply-phase errors
        # carry their detail in the apply log (#419). Plan-phase errors
        # use the plan log as before. apply_started_at is the
        # discriminator: set as soon as the apply phase begins, never
        # unset.
        if run.apply_started_at is not None:
            key = apply_log_key(str(run.workspace_id), str(run.id))
            primary_label = "APPLY_LOG"
        else:
            key = plan_log_key(str(run.workspace_id), str(run.id))
            primary_label = "PLAN_LOG"
        primary_lang = "text"
        try:
            raw = await storage.get(key)
        except Exception as e:
            logger.warning(
                "log not available for failure_analysis",
                run_id=str(run.id),
                label=primary_label,
                error=str(e),
            )
            return "", primary_label, primary_lang, "", ""
        primary = _truncate_tail(raw, cfg.plan_json_max_bytes)

    cur_tarball: bytes | None = None
    code_context = ""
    if (
        cfg.code_context_max_bytes > 0 or cfg.code_diff_max_bytes > 0
    ) and run.configuration_version_id:
        cv_key = config_version_key(str(run.workspace_id), str(run.configuration_version_id))
        try:
            cur_tarball = await storage.get(cv_key)
        except Exception as e:
            logger.debug("Current CV tarball not available", error=str(e))
            cur_tarball = None
        if cur_tarball is not None and cfg.code_context_max_bytes > 0:
            try:
                code_context = await asyncio.to_thread(
                    _extract_tf_sources, cur_tarball, cfg.code_context_max_bytes
                )
            except Exception as e:
                logger.debug("Failed to extract code_context", error=str(e))

    code_diff = ""
    if cfg.code_diff_max_bytes > 0 and cur_tarball is not None:
        prev_cv_id = await _find_previously_applied_cv_id(db, run.workspace_id, run.id)
        if prev_cv_id is not None:
            prev_key = config_version_key(str(run.workspace_id), str(prev_cv_id))
            try:
                prev_tarball = await storage.get(prev_key)
            except Exception as e:
                logger.debug("Previous CV tarball not available (likely GC'd)", error=str(e))
                prev_tarball = None
            if prev_tarball is not None:
                try:
                    code_diff = await asyncio.to_thread(
                        _build_code_diff, prev_tarball, cur_tarball, cfg.code_diff_max_bytes
                    )
                except Exception as e:
                    logger.debug("Failed to build code_diff", error=str(e))

    return primary, primary_label, primary_lang, code_context, code_diff


# --- Daily budget ------------------------------------------------------------


def _budget_key() -> str:
    today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    return f"tp:ai_summary:budget:{today}"


async def _budget_remaining() -> int | None:
    """Return remaining output-token budget, or None if unlimited."""
    cfg = settings.ai_summary
    if cfg.daily_token_budget <= 0:
        return None
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    spent_raw = await r.get(_budget_key())
    spent = int(spent_raw) if spent_raw else 0
    return max(0, cfg.daily_token_budget - spent)


async def _budget_charge(tokens: int) -> None:
    cfg = settings.ai_summary
    if cfg.daily_token_budget <= 0 or tokens <= 0:
        return
    from terrapod.redis.client import get_redis_client

    r = get_redis_client()
    pipe = r.pipeline(transaction=False)
    pipe.incrby(_budget_key(), tokens)
    pipe.expire(_budget_key(), 60 * 60 * 36)  # span at least one UTC day
    await pipe.execute()


# --- Workspace mode resolution ----------------------------------------------


def _resolve_workspace_mode(ws: Workspace) -> bool:
    """Resolve the 3-state per-workspace toggle against the global flag.

    Truth table (global × workspace):
      enabled × default  → ON
      enabled × enabled  → ON  (UX may not even surface this state)
      enabled × disabled → OFF
      disabled × default  → OFF
      disabled × enabled  → OFF  (global wins; UX hides this state)
      disabled × disabled → OFF
    """
    if not settings.ai_summary.enabled:
        return False
    return ws.ai_summary_mode != "disabled"


# --- Model call --------------------------------------------------------------


def _supports_anthropic_cache_control(model: str) -> bool:
    """Whether the model honours ``cache_control: {"type": "ephemeral"}``
    blocks for prompt caching.

    Three families take the marker:
      - ``anthropic/<id>`` — Anthropic direct.
      - ``bedrock/[us\\.|eu\\.]anthropic.<id>`` and any
        ``bedrock/.*claude.*`` — Anthropic models on Bedrock.
      - ``bedrock/[us\\.|eu\\.]amazon.nova-*`` — Amazon Nova on
        Bedrock (Nova Pro / Lite both support the same marker).

    Other providers either cache automatically given a long enough
    repeated prefix (OpenAI direct, DeepSeek direct) or don't cache
    at all (Gemini, Azure OpenAI on older deployments, Bedrock →
    Llama / Mistral / Cohere, Groq, self-hosted vLLM). In both cases
    no marker is needed; the request still works.

    Detection is by model-string prefix only — same routing surface
    LiteLLM uses to pick the provider. No live capability probing.
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith("anthropic/"):
        return True
    if m.startswith("bedrock/"):
        tail = m[len("bedrock/") :]
        # Bedrock cross-region inference prefixes its model IDs with
        # ``us.`` / ``eu.`` / ``apac.``. Strip them so the family
        # check below matches on the same shape as direct-region IDs.
        for prefix in ("us.", "eu.", "apac.", "ap-southeast.", "ap-northeast."):
            if tail.startswith(prefix):
                tail = tail[len(prefix) :]
                break
        if tail.startswith("anthropic.") or "claude" in tail:
            return True
        if tail.startswith("amazon.nova-") or tail.startswith("nova-"):
            return True
    return False


def _apply_anthropic_cache_markers(messages: list[dict]) -> list[dict]:
    """Mark the system + initial user message for ephemeral caching.

    The Anthropic / Bedrock-Anthropic / Bedrock-Nova prompt-caching
    protocol takes plain string ``content`` and rewrites it into a
    one-element list of content blocks with ``cache_control`` on the
    last (and only) block. The cacheable prefix is everything up to
    and including the marked block.

    Two markers are emitted: one on the system prompt (mostly static
    skill + style instructions) and one on the initial user message
    (carries the plan JSON + code diff — the bulk of the prompt).
    Everything after that — follow-up user / assistant turns — is
    uncached and re-sent each turn, which is fine: those payloads
    are small.

    The cached prefix must be byte-identical across turns or the
    provider hashes a different key and the cache misses. Callers
    must not sneak per-turn timestamps / nonces into the cached
    blocks.
    """
    if not messages:
        return messages
    out: list[dict] = []
    seen_user = False
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        # Mark the system prompt + the FIRST user message only;
        # subsequent user / assistant turns stay plain string and
        # land after the cacheable prefix.
        if role == "system" or (role == "user" and not seen_user):
            if isinstance(content, str):
                rewritten = dict(msg)
                rewritten["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                out.append(rewritten)
            else:
                out.append(msg)
            if role == "user":
                seen_user = True
        else:
            out.append(msg)
    return out


def _build_litellm_kwargs(
    *,
    kind: str,
    system_message: str,
    user_message: str,
    max_output_tokens: int,
    history: list[dict] | None = None,
    use_tools: bool = True,
) -> dict:
    """Assemble the keyword arguments for ``litellm.acompletion``.

    Includes the ``tools`` definition and a forcing ``tool_choice`` so
    the provider returns structured output via its native tool-calling
    surface (Bedrock Converse toolConfig, OpenAI function calling,
    Anthropic direct, etc.). LiteLLM translates per provider; we always
    write OpenAI-shape on the way in.

    Provider-specific keys (``api_key``, ``api_base``, ``aws_*``) are
    passed through unconditionally — LiteLLM ignores the ones that
    don't apply to the resolved provider, so this keeps the dispatch
    table flat (no per-provider branching in Terrapod).

    ``history``: optional chronologically-ordered list of
    ``{"role": ..., "content": ...}`` dicts appended after the
    cacheable prefix. Used by the follow-up chat path (#463) to feed
    prior turns back to the model. The cacheable prefix (system +
    initial user) stays first so prompt caching kicks in for every
    turn against the same plan.

    ``use_tools``: when False, omits the ``tools`` definition and
    ``tool_choice`` from the request. Used by the chat follow-up
    path where we want prose replies, not structured output. The
    initial-user message still contains the prompt's "call the
    tool" instruction — that's referring to the FIRST turn (already
    answered as the synthesised assistant turn-0); the model handles
    multi-turn correctly even with that instruction in the prefix.
    """
    cfg = settings.ai_summary
    auth = cfg.auth

    messages: list[dict] = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]
    if history:
        messages.extend(history)
    if _supports_anthropic_cache_control(cfg.model):
        messages = _apply_anthropic_cache_markers(messages)

    kwargs: dict = {
        "model": cfg.model,
        "max_tokens": max_output_tokens,
        "messages": messages,
        "timeout": cfg.request_timeout_seconds,
    }

    if use_tools:
        tool = tool_for_kind(kind)
        tool_name = tool["function"]["name"]
        kwargs["tools"] = [tool]
        # Force the named tool — without this, models can choose to
        # respond in plain prose and we lose the schema guarantee.
        kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_name}}

    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    if auth.api_key:
        kwargs["api_key"] = auth.api_key

    # AWS / Bedrock — LiteLLM only honours these when the model is
    # ``bedrock/...``; safe to send unconditionally.
    if auth.aws_region:
        kwargs["aws_region_name"] = auth.aws_region
    if auth.aws_role_arn:
        kwargs["aws_role_name"] = auth.aws_role_arn
        kwargs["aws_session_name"] = auth.aws_session_name
        if auth.aws_external_id:
            kwargs["aws_external_id"] = auth.aws_external_id

    return kwargs


async def _call_model(
    *,
    kind: str,
    system_message: str,
    user_message: str,
    max_output_tokens: int,
) -> tuple[dict, int, int]:
    """Drive a tool-calling completion via the LiteLLM library.

    The model is forced (via ``tool_choice``) to call the
    kind-specific submission tool with arguments matching
    ``PLAN_SUMMARY_JSON_SCHEMA``. Provider-side constrained decoding
    (Bedrock Converse, OpenAI function calling, etc.) guarantees the
    arguments are valid JSON — no more mid-string escape bugs.

    Returns ``(parsed_args, input_tokens, output_tokens)``. Raises on
    HTTP errors, missing choices, truncation, or — defensively — if a
    model ignores ``tool_choice`` and returns prose, in which case we
    fall back to parsing the response body as JSON.
    """
    cfg = settings.ai_summary
    if not cfg.model:
        raise RuntimeError("ai_summary.model must be set")

    resp = await litellm.acompletion(
        **_build_litellm_kwargs(
            kind=kind,
            system_message=system_message,
            user_message=user_message,
            max_output_tokens=max_output_tokens,
        )
    )

    if not resp.choices:
        raise RuntimeError("model response had no choices")
    choice = resp.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)

    # Truncation diagnostic stays — applies to both tool args and
    # plain text. `tool_calls` is the finish_reason for a successful
    # tool call on most providers; `stop` is also valid (Bedrock
    # Converse normalises everything to `stop` after a clean tool call
    # in LiteLLM's translation). `length` always means trouble.
    if finish_reason == "length":
        raise RuntimeError(
            f"model response truncated at max_output_tokens={max_output_tokens} "
            f"(finish_reason=length); raise ai_summary.max_output_tokens"
        )

    tool_calls = getattr(choice.message, "tool_calls", None) or []
    if tool_calls:
        parsed = _parse_tool_call_arguments(tool_calls[0])
    else:
        # Defensive fallback: model ignored tool_choice and replied in
        # prose. Shouldn't happen with constrained-decoding providers
        # but keeps us working against self-hosted endpoints that don't
        # support tool calling.
        text = choice.message.content or ""
        logger.warning(
            "Model returned no tool_calls despite tool_choice; "
            "falling back to body-content JSON parse",
            finish_reason=finish_reason,
            response_length=len(text),
        )
        try:
            parsed = _parse_model_json(text)
        except ValueError as e:
            snippet = text if len(text) <= 800 else f"{text[:400]}…{text[-400:]}"
            logger.warning(
                "Summariser body content did not parse as JSON either",
                finish_reason=finish_reason,
                response_length=len(text),
                response_snippet=snippet,
            )
            raise ValueError(f"{e} (finish_reason={finish_reason}, len={len(text)})") from e

    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return parsed, in_tok, out_tok


def _parse_tool_call_arguments(tool_call: object) -> dict:
    """Extract the parsed arguments dict from a LiteLLM tool_call object.

    LiteLLM normalises to OpenAI shape: ``tool_call.function.arguments``
    is the canonical location, and it can come back as either a JSON
    string (most providers) or an already-parsed dict (some translations).
    Handles both, plus the failure modes (missing function attr,
    malformed JSON despite the constrained-decode promise).
    """
    fn = getattr(tool_call, "function", None)
    if fn is None and isinstance(tool_call, dict):
        fn = tool_call.get("function")
    if fn is None:
        raise ValueError("tool_call had no `function` attribute")

    args = getattr(fn, "arguments", None)
    if args is None and isinstance(fn, dict):
        args = fn.get("arguments")
    if args is None:
        raise ValueError("tool_call.function had no `arguments`")

    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError as e:
            snippet = args if len(args) <= 800 else f"{args[:400]}…{args[-400:]}"
            logger.warning(
                "Tool-call arguments were not valid JSON despite tool_choice",
                response_length=len(args),
                response_snippet=snippet,
            )
            raise ValueError(f"tool-call arguments invalid JSON: {e}") from e
    raise ValueError(f"tool_call.function.arguments had unexpected type: {type(args).__name__}")


def _parse_model_json(text: str) -> dict:
    """Parse the model's textual response as a JSON object.

    Permits incidental wrapping (a leading ``"Here's the JSON:"`` line, a
    ```json fenced block, a stray sentence after the object) by trying:

      1. Strict parse of the whole stripped body.
      2. Strip a fenced ```json / ``` block if present.
      3. Extract the first balanced ``{...}`` block.

    The strict schema in the prompt should prevent any of this, but the
    fallbacks are cheap and reduce flakes from chatty models.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip a ```json ... ``` (or plain ``` ... ```) fence — Opus
    # occasionally adds one despite the "no fences" instruction.
    if text.startswith("```"):
        body = text[3:]
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.lstrip("\n")
        end = body.rfind("```")
        if end >= 0:
            try:
                return json.loads(body[:end].strip())
            except json.JSONDecodeError:
                pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("model response was not JSON")


# --- Trigger handler ---------------------------------------------------------


async def _emit_summary_event(
    event: str,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    **extra,
) -> None:
    """Best-effort SSE notify for the per-workspace run-events channel.

    Used by every plan-summary status change so the run-detail page
    updates without a reload (#463 phase 4):

      - ``plan_summary_pending`` — handler dispatched; show spinner.
      - ``plan_summary_ready`` — initial summary landed; render it.
      - ``plan_summary_errored`` — initial summary failed; show error.
      - ``plan_summary_skipped`` — initial summary skipped (budget /
        workspace disabled / runner died); render skipped state.
      - ``plan_summary_message_posted`` — a chat turn landed; refetch
        the transcript so other open browsers see it.

    ``extra`` keys ride the event payload — used by
    ``plan_summary_message_posted`` to carry the new message ID for
    callers that want to scroll-to-message.
    """
    try:
        from terrapod.redis.client import RUN_EVENTS_PREFIX, publish_event

        payload = {
            "event": event,
            "run_id": str(run_id),
            "workspace_id": str(workspace_id),
        }
        payload.update(extra)
        await publish_event(
            f"{RUN_EVENTS_PREFIX}{workspace_id}",
            json.dumps(payload),
        )
    except Exception as e:  # SSE is best-effort
        logger.debug("Failed to publish summary event", event_name=event, error=str(e))


# Backwards-compat shim — existing call sites use _emit_ready_event.
async def _emit_ready_event(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    await _emit_summary_event("plan_summary_ready", workspace_id, run_id)


async def _upsert_summary(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    kind: str,
    status: str,
    description: str = "",
    risk_level: str = "",
    risk_factors: list[dict] | None = None,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str = "",
) -> None:
    """Idempotent upsert keyed on (run_id).

    Never downgrades a 'ready' row — a later errored retry for the same
    run is a no-op so the UI doesn't lose a good summary to a transient
    network blip.
    """
    existing = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
    ).scalar_one_or_none()

    if existing is not None and existing.status == "ready" and status != "ready":
        return

    values = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "description": description,
        "risk_level": risk_level,
        "risk_factors": risk_factors or [],
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error_message": error_message,
        "updated_at": now_utc(),
    }
    stmt = pg_insert(PlanSummary).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "kind": stmt.excluded.kind,
            "status": stmt.excluded.status,
            "description": stmt.excluded.description,
            "risk_level": stmt.excluded.risk_level,
            "risk_factors": stmt.excluded.risk_factors,
            "model": stmt.excluded.model,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


async def handle_ai_plan_summary(payload: dict) -> None:
    """Triggered handler: summarise (or analyse the failure of) one plan.

    Payload: ``{"run_id": "<uuid>", "kind": "plan_summary" | "failure_analysis"}``.
    Enqueued from ``run_service.transition_run`` on plan-phase terminal
    transitions when the feature is globally enabled.
    """
    cfg = settings.ai_summary
    if not cfg.enabled:
        return

    try:
        run_id = uuid.UUID(payload["run_id"])
    except (KeyError, ValueError):
        logger.warning("Invalid ai_plan_summary payload", payload=payload)
        return

    kind = payload.get("kind") or "plan_summary"
    if kind not in {"plan_summary", "failure_analysis"}:
        logger.warning("Invalid ai_plan_summary kind", kind=kind)
        return

    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None:
            return

        ws = (
            await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
        ).scalar_one_or_none()
        if ws is None:
            return

        # Upsert a `pending` row + emit `plan_summary_pending` so the UI
        # shows a placeholder from the moment the handler starts (#463
        # phase 4). Without this the run-detail page silently 404s on
        # /plan-summary until the handler finishes, leaving users
        # wondering if anything is happening. Every exit path below
        # transitions this row to its terminal state and emits the
        # matching event.
        await _upsert_summary(
            db,
            run_id=run_id,
            kind=kind,
            status="pending",
            model=cfg.model,
        )
        await db.commit()
        await _emit_summary_event("plan_summary_pending", ws.id, run_id)

        # Skip summarisation when the runner died abnormally (#430). The plan
        # log + JSON upload happens AFTER the runner posts plan-result, so an
        # OOM / SIGKILL between those steps leaves us with an empty log + the
        # plan resource at status="finished, has-changes=false". Summarising
        # from the empty log produces a confidently-wrong "no changes here"
        # narrative — exactly the failure mode #430 cites. Mark skipped with
        # a clear reason so the UX can show "summary unavailable, runner
        # died" instead of either silence or the misleading summary.
        if run.runner_exit_status in ("oom", "killed"):
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="skipped",
                error_message=f"runner exited abnormally ({run.runner_exit_status})",
            )
            await db.commit()
            await _emit_summary_event("plan_summary_skipped", ws.id, run_id)
            return

        if not _resolve_workspace_mode(ws):
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="skipped",
                error_message="workspace disabled",
            )
            await db.commit()
            await _emit_summary_event("plan_summary_skipped", ws.id, run_id)
            return

        remaining = await _budget_remaining()
        if remaining is not None and remaining <= 0:
            logger.info("AI summariser daily budget exhausted, skipping", run_id=str(run_id))
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="skipped",
                error_message="daily token budget exhausted",
            )
            await db.commit()
            await _emit_summary_event("plan_summary_skipped", ws.id, run_id)
            return

        primary, label, lang, code_context, code_diff = await _gather_inputs(db, run, kind)
        if not primary:
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="errored",
                error_message=f"no {label} available",
            )
            await db.commit()
            await _emit_summary_event("plan_summary_errored", ws.id, run_id)
            return

        system_message, user_message = render_prompt(
            kind=kind,
            fleet_context=cfg.context.fleet_context,
            workspace_context=ws.ai_summary_context,
            primary_input=primary,
            primary_input_label=label,
            primary_input_lang=lang,
            code_context_truncated=code_context,
            code_diff=code_diff,
            prompt_prefix=cfg.context.prompt_prefix,
            prompt_suffix=cfg.context.prompt_suffix,
            state_diverged=bool(ws.state_diverged),
        )

        try:
            parsed, in_tok, out_tok = await _call_model(
                kind=kind,
                system_message=system_message,
                user_message=user_message,
                max_output_tokens=cfg.max_output_tokens,
            )
        except Exception as e:
            logger.warning(
                "AI summariser call failed",
                run_id=str(run_id),
                kind=kind,
                error=str(e),
            )
            await _upsert_summary(
                db,
                run_id=run_id,
                kind=kind,
                status="errored",
                model=cfg.model,
                error_message=str(e)[:500],
            )
            await db.commit()
            await _emit_summary_event("plan_summary_errored", ws.id, run_id)
            return

        description = str(parsed.get("description", ""))[:50_000]
        risk_level = str(parsed.get("risk_level", "")).lower()
        if risk_level not in {"low", "medium", "high", "critical"}:
            risk_level = "low"
        risk_factors = parsed.get("risk_factors") or []
        if not isinstance(risk_factors, list):
            risk_factors = []

        # Telemetry for the prompt rule "empty risk_factors is acceptable
        # ONLY when risk_level == low". The schema enforces presence but
        # not non-emptiness, and constrained decoding can't express the
        # conditional, so the model occasionally returns an elevated
        # risk_level with no enumerated factors. Log it so we can see how
        # often the prompt-level guard fails — no auto-repair yet.
        if risk_level != "low" and not risk_factors:
            logger.warning(
                "summariser.risk_factors_empty_at_elevated_level",
                run_id=str(run_id),
                kind=kind,
                risk_level=risk_level,
                model=cfg.model,
            )

        await _upsert_summary(
            db,
            run_id=run_id,
            kind=kind,
            status="ready",
            description=description,
            risk_level=risk_level,
            risk_factors=risk_factors,
            model=cfg.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        await db.commit()

    # Out-of-transaction side effects
    await _budget_charge(out_tok)
    await _emit_ready_event(ws.id, run_id)

    # PR/MR comment refresh — re-enqueue the existing vcs_commit_status
    # trigger so handle_vcs_commit_status picks up the now-ready
    # PlanSummary and edits it into the per-workspace comment in place.
    # The dedup key includes "aisum" so it doesn't collide with the
    # standard run-state-change enqueues from run_service.
    if run.vcs_pull_request_number:
        try:
            from terrapod.services.scheduler import enqueue_trigger

            await enqueue_trigger(
                "vcs_commit_status",
                {
                    "run_id": str(run_id),
                    "workspace_id": str(ws.id),
                    "target_status": run.status,
                    "has_changes": run.has_changes,
                },
                dedup_key=f"vcs_status:aisum:{run_id}",
                dedup_ttl=60,
            )
        except Exception as e:
            logger.debug("Failed to refresh PR comment after AI summary", error=str(e))

    logger.info(
        "AI summary ready",
        run_id=str(run_id),
        kind=kind,
        risk_level=risk_level,
        in_tok=in_tok,
        out_tok=out_tok,
    )


# ── Follow-up chat (#463) ───────────────────────────────────────────────────


class FollowupError(Exception):
    """Raised by ``post_followup`` for any path the caller must surface
    to the operator (router maps these to HTTP 4xx / 5xx). Pure-domain
    so the service doesn't import FastAPI types."""


class FollowupDisabled(FollowupError):
    """Chat globally off (``ai_summary.enabled=False`` or
    ``followup_max_messages_per_run=0``) or workspace disabled."""


class FollowupCapReached(FollowupError):
    """Run already has ``followup_max_messages_per_run`` user turns."""


class FollowupBudgetExhausted(FollowupError):
    """Daily AI token budget hit before this turn could run."""


async def _build_followup_history(
    db: AsyncSession,
    plan_summary: PlanSummary,
    new_user_text: str,
) -> list[dict]:
    """Return the chat history to append AFTER the cacheable
    (system + initial-user) prefix.

    Layout:
      [0]            assistant — initial summary description (text)
      [1..2N]        prior user / assistant follow-up turns from
                     ``plan_summary_messages`` in chronological order
      [last]         the just-posted user message (caller hasn't
                     committed it yet)

    The initial summary description is rendered as a plain assistant
    text turn rather than synthesising an OpenAI tool_call object —
    keeps history simple, model handles the prose-vs-tool flip fine
    once ``tool_choice`` is omitted from the request. Risk factors are
    deliberately NOT replayed in history; follow-ups rarely need them
    and inflating tokens would defeat the budget gate.
    """

    history: list[dict] = []
    if plan_summary.description:
        history.append({"role": "assistant", "content": plan_summary.description})

    # Mode-switch framing turn. The cacheable prefix's last line tells
    # the model "Now call the submit_plan_summary tool exactly once
    # with your structured answer." Without an explicit hand-off, the
    # model reads any subsequent user turn as off-task and refuses
    # ("I don't answer questions like that — my role here is limited
    # to ... submitting a single structured summary via the tool. I've
    # already done that for this plan."). This synthesised
    # user/assistant exchange establishes follow-up mode — prose
    # replies, no further tool calls — and sits AFTER the cache marker
    # so prompt caching still hits.
    history.append(
        {
            "role": "user",
            "content": (
                "Thanks. The structured summary above has been recorded "
                "via the tool. From here on I'd like to ask follow-up "
                "questions about the plan in plain prose — no more tool "
                "calls. Please answer concisely, grounded in the plan "
                "JSON, code, and diff already provided. If a question "
                "asks for information that isn't in the materials I "
                "shared, say so rather than guessing."
            ),
        }
    )
    history.append(
        {
            "role": "assistant",
            "content": (
                "Understood. I'll answer follow-up questions in prose "
                "based on the plan and the code I've already reviewed. "
                "What would you like to know?"
            ),
        }
    )

    prior = (
        (
            await db.execute(
                sa.select(PlanSummaryMessage)
                .where(PlanSummaryMessage.plan_summary_id == plan_summary.id)
                .order_by(PlanSummaryMessage.created_at, PlanSummaryMessage.id)
            )
        )
        .scalars()
        .all()
    )
    for msg in prior:
        # Skip assistant rows that errored — they have empty content
        # and would just confuse the model. User rows always pass
        # through so the model sees the question they're answering.
        if msg.role == "assistant" and not msg.content.strip():
            continue
        history.append({"role": msg.role, "content": msg.content})

    history.append({"role": "user", "content": new_user_text})
    return history


async def _call_chat_model(
    *,
    system_message: str,
    user_message: str,
    history: list[dict],
    max_output_tokens: int,
) -> tuple[str, int, int]:
    """Drive a prose completion via the LiteLLM library.

    No tools, no tool_choice — follow-ups are text-in / text-out.
    Reuses ``_build_litellm_kwargs`` so the cacheable prefix gets the
    same ``cache_control: ephemeral`` markers the initial summary
    used; the provider's prompt cache amortises plan-context cost
    across every follow-up.

    ``kind`` is irrelevant when ``use_tools=False`` (the tool
    definition isn't included in the request); we pass
    ``"plan_summary"`` for shape only.

    Returns ``(reply_text, input_tokens, output_tokens)``. Raises on
    HTTP failure / truncation / empty response.
    """
    cfg = settings.ai_summary
    if not cfg.model:
        raise RuntimeError("ai_summary.model must be set")

    resp = await litellm.acompletion(
        **_build_litellm_kwargs(
            kind="plan_summary",
            system_message=system_message,
            user_message=user_message,
            max_output_tokens=max_output_tokens,
            history=history,
            use_tools=False,
        )
    )

    if not resp.choices:
        raise RuntimeError("model response had no choices")
    choice = resp.choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise RuntimeError(
            f"chat reply truncated at max_output_tokens={max_output_tokens} "
            "(finish_reason=length); raise ai_summary.followup_max_output_tokens"
        )

    text = (getattr(choice.message, "content", "") or "").strip()
    if not text:
        raise RuntimeError("chat reply was empty")

    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return text, in_tok, out_tok


async def post_followup(
    *,
    db: AsyncSession,
    plan_summary: PlanSummary,
    run: Run,
    workspace: Workspace,
    user_message_text: str,
) -> PlanSummaryMessage:
    """Process a single user follow-up turn (#463).

    The router has already authorised the request and loaded the
    PlanSummary / Run / Workspace rows. This function:

      1. Validates feature flags + per-run cap + daily budget.
      2. Persists the user turn (so it's visible even if the model
         call fails — gives the operator a record of what they
         asked).
      3. Calls the model with the SAME cacheable prefix the initial
         summary used (so caching providers serve the prefix hit).
      4. Persists the assistant turn + telemetry, debits the daily
         budget, commits.

    Returns the persisted assistant ``PlanSummaryMessage`` row.

    Raises:
      FollowupDisabled — chat off (global / workspace), 403/503 surface
      FollowupCapReached — per-run user-turn cap hit, 409 surface
      FollowupBudgetExhausted — daily token budget hit, 429 surface
      RuntimeError / ValueError — model HTTP / parse failures; the
        router maps these to 502 and the user row has already been
        committed so the failure is visible in the transcript with
        an errored assistant row recorded too.
    """

    cfg = settings.ai_summary
    if not cfg.enabled or cfg.followup_max_messages_per_run <= 0:
        raise FollowupDisabled("AI follow-up chat is disabled")
    if not _resolve_workspace_mode(workspace):
        raise FollowupDisabled("AI summary is disabled for this workspace")

    text = (user_message_text or "").strip()
    if not text:
        raise FollowupError("message body is empty")
    # 32 KiB hard cap on a single user turn — defends the DB column
    # and the model's context from a paste-bomb. Generous enough that
    # operators pasting a log excerpt are fine.
    if len(text) > 32 * 1024:
        raise FollowupError("message body exceeds 32 KiB")

    # Per-run user-turn cap. Counts USER rows only — assistant rows
    # don't count, otherwise the cap halves silently.
    user_count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PlanSummaryMessage)
            .where(
                PlanSummaryMessage.plan_summary_id == plan_summary.id,
                PlanSummaryMessage.role == "user",
            )
        )
    ).scalar() or 0
    if user_count >= cfg.followup_max_messages_per_run:
        raise FollowupCapReached(
            f"reached the {cfg.followup_max_messages_per_run}-message cap for this run"
        )

    # Daily token budget. Same accounting as the initial summary —
    # output_tokens against the workspace's daily allowance.
    remaining = await _budget_remaining()
    if remaining is not None and remaining <= 0:
        raise FollowupBudgetExhausted("daily AI token budget exhausted")

    # Persist the user row first so an operator sees their question
    # even if the model call fails downstream.
    user_row = PlanSummaryMessage(
        plan_summary_id=plan_summary.id,
        role="user",
        content=text,
    )
    db.add(user_row)
    await db.flush()

    # Build the cacheable prefix — SAME inputs the initial summary
    # used, so the provider's prompt cache serves the prefix hit.
    primary, label, lang, code_context, code_diff = await _gather_inputs(db, run, plan_summary.kind)
    if not primary:
        # The CV/log was GC'd or never existed. Record an errored
        # assistant row so the transcript is uniform, commit, surface.
        err = f"no {label} available to ground the follow-up"
        assistant_row = PlanSummaryMessage(
            plan_summary_id=plan_summary.id,
            role="assistant",
            content="",
            model=cfg.model,
            error_message=err,
        )
        db.add(assistant_row)
        await db.commit()
        raise FollowupError(err)

    system_message, initial_user_message = render_prompt(
        kind=plan_summary.kind,
        fleet_context=cfg.context.fleet_context,
        workspace_context=workspace.ai_summary_context,
        primary_input=primary,
        primary_input_label=label,
        primary_input_lang=lang,
        code_context_truncated=code_context,
        code_diff=code_diff,
        prompt_prefix=cfg.context.prompt_prefix,
        prompt_suffix=cfg.context.prompt_suffix,
        state_diverged=bool(workspace.state_diverged),
    )
    history = await _build_followup_history(db, plan_summary, text)

    try:
        reply_text, in_tok, out_tok = await _call_chat_model(
            system_message=system_message,
            user_message=initial_user_message,
            history=history,
            max_output_tokens=cfg.followup_max_output_tokens,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "AI follow-up call failed",
            plan_summary_id=str(plan_summary.id),
            run_id=str(run.id),
            error=str(e),
        )
        assistant_row = PlanSummaryMessage(
            plan_summary_id=plan_summary.id,
            role="assistant",
            content="",
            model=cfg.model,
            error_message=str(e)[:500],
        )
        db.add(assistant_row)
        await db.commit()
        raise

    assistant_row = PlanSummaryMessage(
        plan_summary_id=plan_summary.id,
        role="assistant",
        content=reply_text,
        model=cfg.model,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    db.add(assistant_row)
    await _budget_charge(out_tok)
    await db.commit()

    # SSE so other open browsers viewing this run pick up the new
    # turn without a reload. `message_id` lets the client scroll-to.
    await _emit_summary_event(
        "plan_summary_message_posted",
        workspace.id,
        run.id,
        message_id=str(assistant_row.id),
    )

    logger.info(
        "AI follow-up reply",
        plan_summary_id=str(plan_summary.id),
        run_id=str(run.id),
        in_tok=in_tok,
        out_tok=out_tok,
        user_turn=user_count + 1,
    )
    return assistant_row
