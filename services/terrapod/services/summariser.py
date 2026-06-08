"""AI plan summariser + plan-failure analyser (#401).

When enabled via ``ai_summary.enabled``, the API triggers an
asynchronous call after every plan-phase terminal transition:
  - ``planned`` → ``kind=plan_summary``: describe changes + rate risk.
  - ``errored`` while still in the plan phase → ``kind=failure_analysis``:
    explain why the plan failed and suggest fixes.

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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import PlanSummary, Run, Workspace, now_utc
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


def _build_litellm_kwargs(
    *, kind: str, system_message: str, user_message: str, max_output_tokens: int
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
    """
    cfg = settings.ai_summary
    auth = cfg.auth
    tool = tool_for_kind(kind)
    tool_name = tool["function"]["name"]

    kwargs: dict = {
        "model": cfg.model,
        "max_tokens": max_output_tokens,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        "tools": [tool],
        # Force the named tool — without this, models can choose to
        # respond in plain prose and we lose the schema guarantee.
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
        "timeout": cfg.request_timeout_seconds,
    }

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


async def _emit_ready_event(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    try:
        from terrapod.redis.client import RUN_EVENTS_PREFIX, publish_event

        await publish_event(
            f"{RUN_EVENTS_PREFIX}{workspace_id}",
            json.dumps(
                {
                    "event": "plan_summary_ready",
                    "run_id": str(run_id),
                    "workspace_id": str(workspace_id),
                }
            ),
        )
    except Exception as e:  # SSE is best-effort
        logger.debug("Failed to publish plan_summary_ready", error=str(e))


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
            await _emit_ready_event(ws.id, run_id)
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
