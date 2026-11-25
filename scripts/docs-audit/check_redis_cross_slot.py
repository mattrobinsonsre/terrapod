#!/usr/bin/env python3
"""A MULTI/EXEC pipeline must not span Redis keys that hash to different slots.

**Why this is static and not a test.** CI, Tilt, the eval stack and every
developer machine run a SINGLE-NODE Redis, where `transaction=True` across any
number of key prefixes is perfectly legal. The failure exists only on a cluster,
and ElastiCache Serverless -- a mainstream managed choice -- is cluster-mode by
construction with no single-node option. So no test this project can run will
ever catch it, and the only thing that can is a check over the source.

**The class.** The rule is already written down and already honoured in 22
pipelines, with three comments explaining it (`auth/sessions.py`,
`auth/auth_state.py`, `auth/recent_users.py`). It was broken again anyway, in a
new site, by code that had compliant neighbours -- which is the
"compliance by neighbour is not compliance" shape, and why the words needed a
gate behind them. The instance: `vcs_pr_decisions.store_all` wrote
`tp:vcs_pr_eval:...` and `tp:vcs_pr_head:...` in one MULTI/EXEC, every write was
refused with `ClusterCrossSlotError`, and the aggregate commit statuses sat at
"Evaluating affected workspaces" permanently on every pull request (#2708).

**What it proves safe, in order of strength.** A pipeline is accepted when:

1. every command addresses the SAME key expression -- one key cannot be
   cross-slot, and this needs no resolution of what the key is, which is what
   lets it clear the three single-key pipelines in `vcs_aggregate_post`; or
2. the key templates it addresses all carry an IDENTICAL Redis hash tag. Only
   the first `{...}` decides the slot, so equal tags mean one slot.

Anything else is reported. A pipeline whose keys cannot be resolved is reported
rather than assumed safe: a check that cannot vouch for a site should say so.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "services" / "terrapod"

#: Redis commands whose FIRST argument is the key. `mset`-shaped commands take a
#: mapping and are listed separately below so they are reported, never skipped.
_KEYED = {
    "get",
    "set",
    "setex",
    "setnx",
    "getset",
    "append",
    "strlen",
    "incr",
    "incrby",
    "decr",
    "decrby",
    "expire",
    "pexpire",
    "expireat",
    "ttl",
    "pttl",
    "persist",
    "delete",
    "unlink",
    "exists",
    "type",
    "rename",
    "hget",
    "hset",
    "hsetnx",
    "hmset",
    "hmget",
    "hgetall",
    "hdel",
    "hlen",
    "hexists",
    "hincrby",
    "hkeys",
    "hvals",
    "lpush",
    "rpush",
    "lpop",
    "rpop",
    "llen",
    "lrange",
    "lrem",
    "ltrim",
    "lindex",
    "sadd",
    "srem",
    "smembers",
    "scard",
    "sismember",
    "spop",
    "zadd",
    "zrem",
    "zscore",
    "zcard",
    "zrange",
    "zrangebyscore",
    "zremrangebyscore",
    "setbit",
    "getbit",
    "bitcount",
    "xadd",
    "xlen",
    "xrange",
}
#: Commands that take several keys or a mapping, so the first argument is not the
#: whole story. Present in a transactional pipeline, they are always reported.
_MULTIKEY = {"mset", "msetnx", "mget", "sinterstore", "sunionstore", "zunionstore", "rename"}

_MIN_PIPELINES = 4  # a floor: a matcher that stops matching must fail, not pass


def _template(node: ast.AST) -> str | None:
    """The key a node builds, with each interpolation as `<name>`.

    An f-string's literal braces arrive already unescaped in the `Constant`
    parts, so `f"a:{{{x}}}"` yields `a:{` + `<x>` + `}` -- which is exactly the
    hash tag as Redis will see it, and is the whole reason this reads the AST
    rather than the source text.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                inner = part.value
                name = (
                    inner.id
                    if isinstance(inner, ast.Name)
                    else inner.attr
                    if isinstance(inner, ast.Attribute)
                    else "expr"
                )
                out.append(f"<{name}>")
            else:
                return None
        return "".join(out)
    return None


def _hash_tag(tpl: str) -> str | None:
    """The slot-deciding part: between the first `{` and the next `}`."""
    a = tpl.find("{")
    if a == -1:
        return None
    b = tpl.find("}", a + 1)
    return None if b == -1 else tpl[a + 1 : b]


def _prefix(tpl: str) -> str:
    """`tp:<family>` -- what distinguishes one key space from another here."""
    bits = tpl.split(":")
    return ":".join(bits[:2]) if len(bits) >= 2 else tpl


def _builders(tree: ast.AST) -> dict[str, str]:
    """Functions that return a single `tp:` key, mapped to that key's template."""
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rets = [r for r in ast.walk(n) if isinstance(r, ast.Return) and r.value is not None]
        if len(rets) != 1:
            continue
        tpl = _template(rets[0].value)
        if tpl and tpl.startswith("tp:"):
            out[n.name] = tpl
    return out


def _enclosing(tree: ast.AST, lineno: int):
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.lineno <= lineno <= (n.end_lineno or n.lineno):
                if best is None or n.lineno > best.lineno:
                    best = n
    return best


def _pipeline_sites(tree: ast.AST):
    """Every `*.pipeline(transaction=True)` call."""
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr != "pipeline":
            continue
        for kw in n.keywords:
            if (
                kw.arg == "transaction"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                yield n


def _keys_touched(fn, builders: dict[str, str]):
    """(distinct key expressions, resolved templates, multikey commands seen)."""
    local: dict[str, str] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = n.value
            if isinstance(v, ast.Call):
                fname = v.func.id if isinstance(v.func, ast.Name) else getattr(v.func, "attr", None)
                if fname in builders:
                    local[n.targets[0].id] = builders[fname]
            else:
                tpl = _template(v)
                if tpl and tpl.startswith("tp:"):
                    local[n.targets[0].id] = tpl

    exprs: set[str] = set()
    templates: set[str] = set()
    multikey: set[str] = set()
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        recv = n.func.value
        if not (isinstance(recv, ast.Name) and recv.id in ("pipe", "pipeline", "p", "tx")):
            continue
        cmd = n.func.attr
        if cmd in _MULTIKEY:
            multikey.add(cmd)
        if cmd not in _KEYED and cmd not in _MULTIKEY:
            continue
        if not n.args:
            continue
        first = n.args[0]
        exprs.add(ast.dump(first))
        if isinstance(first, ast.Name) and first.id in local:
            templates.add(local[first.id])
        else:
            tpl = _template(first)
            if tpl:
                templates.add(tpl)
            elif isinstance(first, ast.Call):
                fname = (
                    first.func.id
                    if isinstance(first.func, ast.Name)
                    else getattr(first.func, "attr", None)
                )
                if fname in builders:
                    templates.add(builders[fname])
    return exprs, templates, multikey


def _verdict(exprs: set[str], templates: set[str], multikey: set[str]) -> str | None:
    """None when safe; otherwise why not."""
    if multikey:
        return f"uses multi-key command(s) {sorted(multikey)} inside a transaction"
    if len(exprs) <= 1:
        return None  # one key expression cannot be cross-slot
    prefixes = {_prefix(t) for t in templates}
    if len(templates) < len(exprs):
        return (
            f"addresses {len(exprs)} distinct key expressions but only "
            f"{len(templates)} could be resolved, so it cannot be vouched for"
        )
    if len(prefixes) <= 1:
        return None
    tags = {_hash_tag(t) for t in templates}
    if len(tags) == 1 and None not in tags:
        return None  # one shared hash tag -> one slot
    return (
        f"spans key prefixes {sorted(prefixes)} with hash tags {sorted(repr(t) for t in tags)} "
        f"-- different slots in cluster mode, so EXEC is refused with ClusterCrossSlotError"
    )


# ---------------------------------------------------------------------------
# Self-test on literal fixtures, asserted BEFORE the tree is scanned. Two of the
# fixtures this project wrote first were vacuous because they asserted on the
# scan's OUTPUT, where another clause filtered the case out regardless -- so each
# clause is exercised here on source of its own.
# ---------------------------------------------------------------------------
_FIXTURES: list[tuple[str, str, bool]] = [
    (
        "cross-slot: two prefixes, no tags",
        """
def ka(s): return f"tp:aaa:{s}"
def kb(s): return f"tp:bbb:{s}"
async def f(redis, s):
    pipe = redis.pipeline(transaction=True)
    a = ka(s); b = kb(s)
    pipe.hset(a, "x", "1"); pipe.expire(b, 60)
    await pipe.execute()
""",
        False,
    ),
    (
        "safe: two prefixes sharing one hash tag",
        """
def ka(s): return f"tp:aaa:{{{s}}}:z"
def kb(s): return f"tp:bbb:{{{s}}}"
async def f(redis, s):
    pipe = redis.pipeline(transaction=True)
    a = ka(s); b = kb(s)
    pipe.hset(a, "x", "1"); pipe.expire(b, 60)
    await pipe.execute()
""",
        True,
    ),
    (
        "safe: one key expression, unresolvable (a parameter)",
        """
async def f(redis, key):
    pipe = redis.pipeline(transaction=True)
    pipe.hgetall(key); pipe.delete(key)
    await pipe.execute()
""",
        True,
    ),
    (
        "cross-slot: tags present but DIFFERENT",
        """
def ka(s): return f"tp:aaa:{{{s}}}:z"
def kb(s): return f"tp:bbb:{{other}}"
async def f(redis, s):
    pipe = redis.pipeline(transaction=True)
    a = ka(s); b = kb(s)
    pipe.hset(a, "x", "1"); pipe.expire(b, 60)
    await pipe.execute()
""",
        False,
    ),
    (
        "reported: two expressions, one unresolvable",
        """
def ka(s): return f"tp:aaa:{s}"
async def f(redis, s, other):
    pipe = redis.pipeline(transaction=True)
    a = ka(s)
    pipe.hset(a, "x", "1"); pipe.expire(other, 60)
    await pipe.execute()
""",
        False,
    ),
    (
        "safe: not transactional at all",
        """
def ka(s): return f"tp:aaa:{s}"
def kb(s): return f"tp:bbb:{s}"
async def f(redis, s):
    pipe = redis.pipeline(transaction=False)
    pipe.hset(ka(s), "x", "1"); pipe.expire(kb(s), 60)
    await pipe.execute()
""",
        True,
    ),
    (
        "cross-slot: a multi-key command inside a transaction",
        """
async def f(redis, key):
    pipe = redis.pipeline(transaction=True)
    pipe.mset({"tp:a": 1, "tp:b": 2})
    await pipe.execute()
""",
        False,
    ),
]


def _self_test() -> None:
    for name, source, expect_safe in _FIXTURES:
        tree = ast.parse(source)
        builders = _builders(tree)
        sites = list(_pipeline_sites(tree))
        if not sites:
            assert expect_safe, f"self-test {name!r}: no transactional pipeline found"
            continue
        problems = []
        for site in sites:
            fn = _enclosing(tree, site.lineno)
            assert fn is not None, f"self-test {name!r}: no enclosing function"
            why = _verdict(*_keys_touched(fn, builders))
            if why:
                problems.append(why)
        safe = not problems
        assert safe == expect_safe, (
            f"self-test {name!r}: expected {'safe' if expect_safe else 'a finding'}, "
            f"got {problems or 'safe'}"
        )
    # The tag reader is the subtlest part, so pin it directly too.
    assert _hash_tag("tp:a:{x:y}:z") == "x:y", _hash_tag("tp:a:{x:y}:z")
    assert _hash_tag("tp:a:x:y") is None
    assert _template(ast.parse('f"tp:a:{{{s}:{n}}}:{h}"').body[0].value) == "tp:a:{<s>:<n>}:<h>"


def main() -> int:
    _self_test()

    findings: list[str] = []
    checked = 0
    for path in sorted(SOURCE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            findings.append(f"{path.relative_to(ROOT)}: could not parse ({e})")
            continue
        builders = _builders(tree)
        for site in _pipeline_sites(tree):
            checked += 1
            fn = _enclosing(tree, site.lineno)
            if fn is None:
                findings.append(
                    f"{path.relative_to(ROOT)}:{site.lineno}: a transactional pipeline outside any function"
                )
                continue
            why = _verdict(*_keys_touched(fn, builders))
            if why:
                findings.append(f"{path.relative_to(ROOT)}:{site.lineno}: `{fn.name}` {why}")

    if checked < _MIN_PIPELINES:
        print(
            f"FAIL: inspected only {checked} transactional pipeline(s), below the floor of "
            f"{_MIN_PIPELINES}. The matcher has probably stopped matching -- a check that "
            f"examines nothing passes for the wrong reason.",
            file=sys.stderr,
        )
        return 1

    if findings:
        print(
            "FAIL: MULTI/EXEC pipeline(s) that are cross-slot on a Redis Cluster:\n",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(
            "\n  Give the keys a shared Redis hash tag so they land in one slot "
            '(`f"tp:x:{{{scope}}}:..."`), which keeps the transaction; or drop to '
            "`transaction=False` if atomicity is genuinely not relied upon. See "
            "vcs_pr_decisions.evaluation_key for the worked example and #2708 for why.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {checked} transactional pipeline(s), all single-slot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
