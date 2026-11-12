"""Every runner setting the API reads must reach the API pod (#2520).

`RunnerConfig` is the listener's config object, and the API reads a handful of
its fields too -- the run reconciler's timeouts, the lifecycle- and
configure-retry cycles, the runner-token TTLs, the execution-hooks kill-switch.
It reads them through the same `load_runner_config()`, whose file source is
`/etc/terrapod/runners.yaml`.

That file reached the API only by accident of co-location. `configmap-runner.yaml`
is wrapped in ``{{- if .Values.listener.enabled }}``, the API Deployment never
mounted it, and in an API-only release it is not even rendered -- so in the
documented multi-cluster layout (API with ``listener.enabled: false``, agents
installed separately) every one of those settings silently fell back to its code
default. An operator who set `runners.configureNoProgressSeconds` in the API's
release rendered nothing; one who set it in an agent release changed a file the
API never reads. Nothing reported that the value had gone nowhere.

**This is leg 3 of the config-channel contract**, and it is the leg that fails
silently: the key is in values.yaml, it is declared in values.schema.json, the
chart renders and the pod starts, and the setting does nothing.

**The derivation is the point of this file.** The existing gates cannot see this
class, each for the same reason: they derive from a place that already presumes
the right ConfigMap. `test_values_contract` snapshots the keys in values.yaml;
`test_config_contract` snapshots the keys `RunnerConfig` accepts;
`helm-smoke`'s retention check derives from values.yaml too. So a field that is
in values.yaml, in the model, and rendered into the *wrong* ConfigMap is outside
all three subjects at once. This one derives from **what the API's own code
reads**, which is the only source that cannot omit a field and still be the
thing it describes.

Reads the template source rather than rendered output, for the reason
`test_retention_settings_are_chart_reachable` gives: `docker/Dockerfile.test`
ships the chart and no helm binary, so a gate that shells out to helm can only
skip in CI, which is how a gate becomes decorative. The rendered-output
assertion -- that the value is the operator's and not the default -- is the
`helm-smoke` step "A runner setting the API reads reaches the API pod".
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

HERE = pathlib.Path(__file__).resolve()

#: Below this the walker has stopped finding call sites and the gate is reading
#: nothing. Six modules outside `runner/` call `load_runner_config` today.
MIN_FIELDS = 8

#: The functions that hand back a `RunnerConfig`. `_lrc` is a local alias one
#: call site imports it under.
_LOADERS = {"load_runner_config", "_lrc"}


def _root() -> pathlib.Path | None:
    for cand in HERE.parents:
        if (cand / "helm" / "terrapod" / "Chart.yaml").is_file():
            return cand
    return None


ROOT = _root()
pytestmark = pytest.mark.skipif(ROOT is None, reason="chart not present in this image")


def _called(node: ast.AST) -> bool:
    """True when `node` is a call to one of the loader functions."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in _LOADERS


def _fields_read_by(tree: ast.AST) -> set[str]:
    """Every `RunnerConfig` attribute this module reads off a loaded config.

    Two shapes, because both are in the tree: the config bound to a name
    (``cfg = load_runner_config()`` then ``cfg.stale_timeout_seconds``) and read
    straight off the call (``load_runner_config().hooks_enabled``). Scoped per
    function so a name bound in one function cannot pick up an unrelated
    attribute of the same name in another.
    """
    fields: set[str] = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            continue
        names: set[str] = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and _called(node.value):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            if isinstance(node, ast.Attribute) and _called(node.value):
                fields.add(node.attr)
        if not names:
            continue
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in names
            ):
                fields.add(node.attr)
    return fields


def api_read_fields() -> set[str]:
    """The `RunnerConfig` fields the API process reads.

    Everything under `services/terrapod` except `runner/`, which is the listener
    and the runner Job -- they read the whole object from their own ConfigMap and
    are not what this gate is about. `config.py` itself only defines the loader.
    """
    # The test image FLATTENS `services/` into /app (`COPY services/terrapod
    # ./terrapod` with `WORKDIR /app`), so the repo-relative path exists only
    # in a checkout. Getting this wrong does not skip -- the chart IS in the
    # image, so `ROOT` resolves and the walker simply reads an empty directory
    # and derives nothing. The MIN_FIELDS floor below is what caught it, which
    # is the floor doing its job; this keeps it from having to.
    pkg = pathlib.Path("/app/terrapod")
    if not pkg.is_dir():
        pkg = ROOT / "services" / "terrapod"
    assert pkg.is_dir(), f"package not found at {pkg} -- the image layout moved"
    fields: set[str] = set()
    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(pkg)
        if rel.parts[0] == "runner" or rel.name == "config.py":
            continue
        src = path.read_text()
        if "load_runner_config" not in src:
            continue
        fields |= _fields_read_by(ast.parse(src))
    assert len(fields) >= MIN_FIELDS, (
        f"only {len(fields)} fields derived ({sorted(fields)}) -- the walker has "
        "stopped matching the call sites, so this gate is reading nothing. Check "
        "whether `load_runner_config` is now reached under a different name."
    )
    return fields


def _api_runners_block() -> str:
    """The `runners.yaml` literal block of configmap-api.yaml."""
    tpl = (ROOT / "helm/terrapod/templates/configmap-api.yaml").read_text()
    assert "runners.yaml: |" in tpl, (
        "configmap-api.yaml renders no `runners.yaml` key, so NONE of the runner "
        "settings the API reads reach it (#2520). The API mounts this ConfigMap at "
        "/etc/terrapod, which is where `load_runner_config` looks."
    )
    return tpl.split("runners.yaml: |", 1)[1]


def test_every_runner_setting_the_api_reads_has_a_render_line() -> None:
    block = _api_runners_block()
    missing = sorted(f for f in api_read_fields() if f not in block)
    assert not missing, (
        "the API reads these runner settings and the chart never delivers them to "
        "it, so an operator's value is silently replaced by the code default -- and "
        "with `listener.enabled: false` there is no other ConfigMap that could. Add "
        f"a render line to the `runners.yaml` block of configmap-api.yaml: {missing}"
    )


def test_the_api_block_spells_every_key_the_way_the_runner_block_does() -> None:
    """One key, two ConfigMaps, one value -- or a combined release disagrees.

    In a release with both components these fields are rendered twice, from the
    same `.Values.runners.*`. If the two expressions drift, the reconciler and
    the listener act on different numbers for the same setting and nothing says
    so: both ConfigMaps are valid and both pods start.
    """
    api = _api_runners_block()
    runner = (ROOT / "helm/terrapod/templates/configmap-runner.yaml").read_text()
    fields = api_read_fields()
    runner_lines = {ln.strip() for ln in runner.splitlines()}
    mismatched: list[str] = []
    for line in api.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("{{") or ":" not in stripped:
            continue
        if stripped.split(":", 1)[0] not in fields:
            continue
        if stripped not in runner_lines:
            mismatched.append(stripped)
    assert not mismatched, (
        "these lines render differently in configmap-api.yaml and "
        "configmap-runner.yaml, so a combined release gives the API and the "
        "listener different values for one setting:\n  " + "\n  ".join(mismatched)
    )


def test_no_zero_meaning_setting_is_rendered_with_a_swallowing_default() -> None:
    """`X | default N` discards an operator's 0, exactly as `| default true`
    discards a `false`.

    Go templates treat 0 as empty, so `{{ .Values.runners.driftMaxDurationSeconds
    | default 1800 }}` renders 1800 both when the key is unset AND when the
    operator set it to 0. Three of these settings document 0 as the way to
    DISABLE them, so each was impossible to switch off -- accepted by `helm
    lint`, through the schema, and then ignored. `dig` returns the stored value
    including 0, which is why the chart already uses it for `hooksEnabled`.

    Scoped to the keys this change renders in two places rather than to every
    `| default` in the chart: most of those sit on settings where 0 has no
    meaning (a pool size, an SMTP port), and a blanket ban would be a large
    unrelated refactor whose findings were mostly not defects.
    """
    fields = api_read_fields()
    offenders: list[str] = []
    for name in ("configmap-api.yaml", "configmap-runner.yaml"):
        tpl = ROOT / "helm/terrapod/templates" / name
        for number, line in enumerate(tpl.read_text().splitlines(), start=1):
            match = re.match(r"\s*([a-z_]+):\s*\{\{.*\|\s*default\s+-?\d+\s*\}\}", line)
            if match and match.group(1) in fields:
                offenders.append(f"{name}:{number}  {line.strip()}")
    assert not offenders, (
        "`| default <number>` renders the default when the operator set the value "
        "to 0, because Go templates treat 0 as empty. Use `dig` instead, which "
        "returns a stored 0:\n"
        '  {{ dig "someKey" 1800 .Values.runners | int64 }}\n' + "\n".join(offenders)
    )


def test_the_configure_scheduling_block_renders_only_real_model_fields() -> None:
    """A key `RunnerPhaseScheduling` does not model is dropped in SILENCE (#2524).

    A nested pydantic model ignores extras, so a render line misspelling
    `node_selector` is accepted by `helm lint`, renders valid YAML, parses into a
    valid config, and the operator's scheduling is simply never applied -- the
    exact failure mode of #2520 one level down. Derived from the model so a field
    added there has to be rendered rather than silently forgotten, and in the
    other direction so a rendered key has to be a field.

    The `runners.configure` VALUES keys are camelCase and the rendered
    `runners.yaml` keys are snake_case, which is why this reads the rendered key
    names rather than the `.Values` paths beside them.
    """
    from terrapod.config import RunnerPhaseScheduling

    tpl = (ROOT / "helm/terrapod/templates/configmap-runner.yaml").read_text()
    assert "\n    configure:\n" in tpl, (
        "configmap-runner.yaml renders no `configure:` block, so the per-phase "
        "scheduling never reaches the listener that builds the Job (#2524)"
    )
    block = tpl.split("\n    configure:\n", 1)[1].split("\n    {{- end }}", 1)[0]
    rendered = {
        line.strip().rstrip(":")
        for line in block.splitlines()
        if line.startswith("      ") and line.strip().endswith(":") and "{{" not in line
    }
    fields = set(RunnerPhaseScheduling.model_fields)
    assert rendered == fields, (
        "the `configure:` block in configmap-runner.yaml and "
        "RunnerPhaseScheduling disagree.\n"
        f"  modelled but never rendered (an operator cannot set these): "
        f"{sorted(fields - rendered)}\n"
        f"  rendered but not a field (DROPPED in silence by the model): "
        f"{sorted(rendered - fields)}"
    )
