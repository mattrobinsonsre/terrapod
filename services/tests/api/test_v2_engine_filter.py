"""The TFE surface never serves a non-Terraform row (#1407 §2, #1487, #1572).

A source-introspection test, which is the right shape here: the invariant is
"every workspace/run lookup reachable from the TFE surface carries the engine
filter", and that is a property of the *source*, not of any one response.

**This test used to read one file, while the property spans nine router mounts.**
`tfe_v2.py` was scoped and the other eight were not, so one workspace answered two
ways — `GET /api/tfe/v2/workspaces/{id}` said 404 for a Pulumi workspace and
`GET /api/tfe/v2/workspaces/{id}/runs` said 200. The guard was well built and
pointed at a quarter of its own subject.

So the file list is now **derived from `app.py`'s `include_tfe(...)` calls**
rather than written down here. A router mounted on that surface in future is
covered without anyone remembering to add it, which is the only version of this
that stays true. A future edit that adds a
query without the filter is exactly what this catches, and nothing else would —
with Terraform the only engine, every runtime assertion passes either way.

The failure it guards against is silent. A `terraform` CLI handed a Pulumi
workspace does not get an error; it gets a workspace it cannot parse.

**The two exceptions are load-bearing.** `workspaces.name` is unique *globally*,
across engines (`uq_workspaces`), so the name-uniqueness guards must NOT filter:
one that did would decide a taken name was free and then hit an IntegrityError,
turning a clean 422 into a 500. They are allow-listed by name here so that
reasoning survives — rather than being invisibly absent from the list.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

API = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api"
ROUTER = API / "routers/tfe_v2.py"


def _tfe_mounted_modules() -> list[pathlib.Path]:
    """Every router module mounted on the TFE surface, read from `app.py`.

    Derived rather than listed: a hand-maintained list is exactly what was
    missing before, and it would go stale the first time someone mounted a
    router without reading this file.

    `engine_scope.py` joins them because the shared loader lives there — the
    routers delegate their workspace lookup to it, so its query is the one that
    has to carry the filter. Without it the guard would pass trivially for every
    router that delegates, which is the shape of "green for the wrong reason".
    """
    tree = ast.parse((API / "app.py").read_text())
    mounted = {
        n.args[0].id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "include_tfe"
        and n.args
        and isinstance(n.args[0], ast.Name)
    }
    modules = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and ".routers." in f"{n.module}.":
            for a in n.names:
                if (a.asname or a.name) in mounted:
                    modules.add(n.module.rsplit(".", 1)[-1])
    paths = [API / "routers" / f"{m}.py" for m in sorted(modules)]
    assert paths, "no TFE-mounted routers found — the app.py parse has drifted"
    return [*paths, API / "engine_scope.py"]


#: Models whose rows are engine-scoped, so a V2 query must not return another
#: engine's.
GUARDED = {"Workspace", "Run", "ConfigurationVersion"}

#: Queries that deliberately span every engine, and why. A name-uniqueness check
#: is asking "is this name taken anywhere", which is what the database constraint
#: enforces; scoping it to one engine would make it answer a different question
#: from the one the constraint asks.
#: Keyed by module, because the exceptions are specific to the file they sit in
#: and a flat set would silently excuse a same-named function elsewhere.
UNSCOPED_BY_DESIGN: dict[str, dict[str, str]] = {
    "policy_checks.py": {
        # A Run carries no engine column — `_engine_filter(Run)` is a subquery
        # through its workspace for exactly that reason — so a run lookup cannot
        # be filtered in place. The check is two lines below, on the workspace,
        # and `test_run_lookups_are_scoped_at_their_chokepoint` asserts it is
        # really there rather than letting this entry excuse nothing.
        "_run_and_caps": "runs have no engine column; scoped via the workspace below",
    },
    "registry_modules.py": {
        # Links a registry module to a client-named workspace. It writes a
        # relationship and returns no workspace data, so nothing about another
        # engine's row reaches the caller — the operation is merely reachable
        # from a door it has no business being behind, which is a tidiness
        # question rather than a leak.
        "create_workspace_link": "write that references a workspace, returns none of it",
    },
    "variables.py": {
        "add_varset_workspaces": "write that references a workspace, returns none of it",
    },
    "runs.py": {
        # The CV id comes from the run-create body, and the run is created on a
        # workspace that IS scoped just above. Neither lookup returns CV data;
        # they decide `plan_only` and whether to queue. Worth noting that
        # ownership of the CV is not checked against the workspace either — a
        # separate concern from engine scoping, and not one this guard is about.
        "create_run": "decides queueing from a CV; returns no CV data",
        # Loads the owning workspace of a run that has already been resolved and
        # authorized, which is where the engine check itself now lives.
        "_resolve_cost_summary_for_chat": "workspace of an already-authorized run",
        "_resolve_plan_summary_for_chat": "workspace of an already-authorized run",
    },
    "config_versions.py": {
        # Both hang off a workspace that `_get_workspace` already scoped, so the
        # rows reachable here belong to a workspace this surface may see. Listed
        # rather than given a redundant filter: a second filter on a derived
        # query reads as though the parent were untrusted, and the next person
        # would wonder which one is load-bearing.
        "list_configuration_versions": "rows of an already-scoped workspace",
        "upload_configuration": "runs of an already-scoped configuration version",
    },
    "tfe_v2.py": {
        # The create body moved here when the native surface gained an `engine`
        # (#1535); the route above it is now a thin wrapper holding no query. The
        # reasoning is unchanged and is now more load-bearing, not less: this
        # function creates workspaces for EVERY engine, so a name taken by a
        # Pulumi workspace has to conflict here too.
        "_create_workspace_impl": "name-uniqueness guard against a globally unique constraint",
        "update_workspace": "rename uniqueness guard against a globally unique constraint",
    },
}


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or (node.end_lineno - node.lineno) < (
                    best.end_lineno - best.lineno
                ):
                    best = node
    return best.name if best else "<module>"


def _select_calls(tree: ast.AST):
    """Every engine-scoped lookup: `select(Model)` AND `db.get(Model, pk)`.

    `db.get` was the gap that let #1904 through. A primary-key load reads no more
    safely than a query — it just reads shorter, and the guard could not see it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_select = isinstance(node.func, ast.Name) and node.func.id == "select"
        is_get = isinstance(node.func, ast.Attribute) and node.func.attr == "get"
        if not (is_select or is_get):
            continue

        # `db.get(Model, X)` is only interesting when X came from the CLIENT.
        # `db.get(Workspace, run.workspace_id)` derives from a row that has
        # already been loaded and authorized, so it inherits that row's scoping —
        # flagging it would mean twenty allow-list entries saying the same thing,
        # which trains people to add entries instead of thinking. A bare name is
        # the dangerous shape: it is a path parameter, and nothing has vouched
        # for it yet.
        if is_get and len(node.args) > 1 and isinstance(node.args[1], ast.Attribute):
            continue

        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in GUARDED:
                yield node, arg.id


def _statement_source(src: str, tree: ast.AST, call: ast.Call) -> str:
    """The whole statement a call sits in.

    Taking the statement rather than the call matters: the filter is applied in a
    chained `.where(...)`, which is a *parent* of the `select(...)` node, so
    looking only at the call itself would never see it.
    """
    lines = src.splitlines()
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            if node.lineno <= call.lineno and (node.end_lineno or node.lineno) >= call.end_lineno:
                if best is None or (node.end_lineno - node.lineno) < (
                    best.end_lineno - best.lineno
                ):
                    best = node
    if best is None:
        return ""
    return "\n".join(lines[best.lineno - 1 : best.end_lineno])


def test_every_tfe_surface_lookup_is_engine_scoped():
    """Across every module mounted on the TFE surface, not just `tfe_v2.py`.

    The marker is the substring `engine_filter`, which matches both the local
    `_engine_filter` in `tfe_v2.py` and the shared `engine_filter` imported from
    `engine_scope`. A router that delegates its lookup to `load_workspace_scoped`
    has no `select(Workspace)` of its own and so has nothing to flag — and that
    is sound, because `engine_scope.py` is itself in the walked set and its query
    must carry the filter.
    """
    unfiltered: list[str] = []
    for path in _tfe_mounted_modules():
        src = path.read_text()
        tree = ast.parse(src)
        allowed = UNSCOPED_BY_DESIGN.get(path.name, {})
        for call, model in _select_calls(tree):
            func = _enclosing_function(tree, call.lineno)
            stmt = _statement_source(src, tree, call)
            if "engine_filter" in stmt:
                continue
            if func in allowed:
                continue
            unfiltered.append(f"{path.name}:{call.lineno} {func}(): select({model}) is unscoped")

    assert not unfiltered, (
        "every workspace/run lookup reachable from the TFE surface must be scoped "
        "to the Terraform engine, or a `terraform` CLI can be handed a row it "
        "cannot parse — and the failure is silent, not an error:\n  "
        + "\n  ".join(unfiltered)
        + "\n\nEither carry `engine_filter(...)` in the statement, or delegate "
        "the lookup to `engine_scope.load_workspace_scoped`, which scopes on the "
        "prefix the request arrived on."
    )


@pytest.mark.parametrize(
    "module,func",
    sorted((m, f) for m, funcs in UNSCOPED_BY_DESIGN.items() for f in funcs),
)
def test_the_allow_listed_guards_still_exist(module: str, func: str):
    """An allow-list entry for a function that no longer exists is a silent hole.

    If `update_workspace` is renamed, its entry stops matching anything and the
    real query underneath it is no longer excused — but nothing would say so, and
    the entry would sit there looking like it still meant something.

    Now keyed by module too, so an entry cannot drift onto a same-named function
    in a different file and quietly excuse the wrong query.
    """
    path = API / "routers" / module
    tree = ast.parse(path.read_text())
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert func in names, (
        f"{module}:{func}() is allow-listed as intentionally engine-unscoped but no "
        "longer exists — remove the entry, or point it at the function that replaced it"
    )


def test_the_filter_helper_is_not_the_auxiliary_run_filter():
    """They mean different things and must not be merged.

    `_primary_run_filter` excludes auxiliary runs from workspace health;
    `_engine_filter` scopes to an engine. Folding them together is how one gets
    removed later for a reason that only applied to the other.
    """
    src = ROUTER.read_text()
    assert "def _engine_filter(" in src
    assert "def _primary_run_filter(" in src
    tree = ast.parse(src)
    engine_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_engine_filter"
    )
    body = ast.dump(engine_fn)
    assert "engine" in body, "_engine_filter must actually compare the engine column"
    assert "source" not in body, "_engine_filter must not have absorbed run-source logic"


@pytest.mark.parametrize(
    "module,func",
    [
        ("runs.py", "_require_run_ws_capability"),
        ("policy_checks.py", "_run_and_caps"),
    ],
)
def test_run_lookups_are_scoped_at_their_chokepoint(module: str, func: str):
    """A run is scoped through its workspace, at the one function every handler
    for that surface already calls.

    This is the other half of the `policy_checks._run_and_caps` allow-list entry.
    A `Run` has no engine column, so the filter cannot sit on the run query; the
    protection is a check on the run's workspace instead. Without this test the
    allow-list entry would be a promise nobody verifies — which is how #1904
    happened in the first place, a rule believed to hold in files nobody checked.

    Asserted on the source rather than a response because the point is
    structural: the check must be at the chokepoint, not in whichever handler
    someone remembered.
    """
    src = (API / "routers" / module).read_text()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "is_tfe_path" in body, (
        f"{module}:{func}() no longer decides on the request's surface — a run "
        "belonging to another engine would be served to a `terraform` client"
    )
    assert "TERRAFORM" in body, f"{module}:{func}() no longer compares the engine"
    assert "404" in body, (
        f"{module}:{func}() should answer 404, not 403 — on the compatibility "
        "surface the run does not exist, and 403 would confirm that it does"
    )


@pytest.mark.parametrize(
    ("module", "func"),
    [("runs.py", "_require_run_ws_capability"), ("policy_checks.py", "_run_and_caps")],
)
def test_every_caller_of_the_chokepoint_hands_it_the_request(module: str, func: str):
    """The chokepoint decides on the request's surface, so every caller must
    give it one.

    `request` is a required keyword there, so a caller that forgets is a
    TypeError — but only on the line that runs, and a route no test exercises
    would ship broken. `confirm_run` shipped exactly that for one commit: it
    took a `Request`, never passed it, and every Pulumi apply on the *native*
    surface would have answered 404 for a run that is perfectly legal there.

    Static, therefore, rather than relying on coverage to find it.
    """
    src = (API / "routers" / module).read_text()
    tree = ast.parse(src)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != func:
            continue
        if not any(kw.arg == "request" for kw in node.keywords):
            enclosing = next(
                (
                    f.name
                    for f in ast.walk(tree)
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
                ),
                "<module>",
            )
            missing.append(f"{enclosing} (line {node.lineno})")
    assert not missing, (
        f"{module}: these call {func}() without passing `request=`, so the "
        f"engine check cannot tell which surface asked: {', '.join(missing)}"
    )


def test_the_api_describes_itself_as_more_than_one_engine():
    """The OpenAPI description is the first orientation an agent gets (#1911).

    It read "Terrapod - Open-source Terraform Enterprise replacement", which is
    true and incomplete in the direction that matters: a client reading it is
    primed to assume one engine, and the surfaces that then 404 give it no clue
    why. Asserted rather than left to a reviewer, because nothing else reads this
    string and prose drifts silently.

    Ansible is deliberately absent — planned, not shipped, and orientation text
    that over-claims is worse than orientation text that under-claims.
    """
    from terrapod.api.app import create_application

    desc = create_application().description
    assert "Pulumi" in desc, "the description names one engine; a client will assume one engine"
    assert (
        desc.index("OpenTofu") < desc.index("Terraform Enterprise") or "OpenTofu/Terraform" in desc
    ), "AGENTS.md: the open-source engine leads in prose"
    assert "Ansible" not in desc, "Ansible is planned, not shipped — do not claim it"
