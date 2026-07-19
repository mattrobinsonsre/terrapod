"""Match-query language — port of OpenInfraQuote's ``oiq_match_query`` (MPL-2.0).

A small boolean query language used by the usage catalogue to select which
default-usage entry applies to a matched product. Grammar (precedence
OR < AND < NOT, matching the upstream menhir grammar)::

    expr    := or
    or      := and ( 'or'  and )*
    and     := not ( 'and' not )*
    not     := 'not' not | atom
    atom    := '(' expr ')' | STRING '=' STRING | STRING

``STRING`` is a bare identifier (any run of chars except whitespace and
``'"()=``) or a quoted ``"…"`` / ``'…'`` literal with ``\\`` escapes. A bare
``STRING`` is a *key-existence* test; ``k = v`` is an equality test.

Evaluation is against a :class:`~terrapod.services.cost.match_set.MatchSet`:
``Equals(k, v)`` holds when the pair is present, ``Key(k)`` when the key is
present with any value. An empty query matches everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from terrapod.services.cost.match_set import MatchSet

# ---------------------------------------------------------------------------
# AST (mirrors oiq_match_query_parser_value.t)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class And:
    left: Node
    right: Node


@dataclass(frozen=True)
class Or:
    left: Node
    right: Node


@dataclass(frozen=True)
class Not:
    inner: Node


@dataclass(frozen=True)
class Equals:
    key: str
    value: str


@dataclass(frozen=True)
class Key:
    key: str


Node = And | Or | Not | Equals | Key


# ---------------------------------------------------------------------------
# Lexer (mirrors oiq_match_query_lexer.ml)
# ---------------------------------------------------------------------------

_SPECIAL = set(" '\"()=")


class _Tok:
    LPAREN = "("
    RPAREN = ")"
    EQUAL = "="
    AND = "and"
    OR = "or"
    NOT = "not"
    EOF = "\x00"


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str = ""


def _tokenize(s: str) -> list[_Token]:
    toks: list[_Token] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            toks.append(_Token(_Tok.LPAREN))
            i += 1
        elif c == ")":
            toks.append(_Token(_Tok.RPAREN))
            i += 1
        elif c == "=":
            toks.append(_Token(_Tok.EQUAL))
            i += 1
        elif c in ("'", '"'):
            stop = c
            i += 1
            buf: list[str] = []
            while i < n and s[i] != stop:
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i + 1])
                    i += 2
                else:
                    buf.append(s[i])
                    i += 1
            if i >= n:
                raise ValueError(f"unterminated string in match query: {s!r}")
            i += 1  # consume closing quote
            toks.append(_Token("STRING", "".join(buf)))
        else:
            start = i
            while i < n and s[i] not in _SPECIAL:
                i += 1
            word = s[start:i]
            if word == "and":
                toks.append(_Token(_Tok.AND))
            elif word == "or":
                toks.append(_Token(_Tok.OR))
            elif word == "not":
                toks.append(_Token(_Tok.NOT))
            else:
                toks.append(_Token("STRING", word))
    toks.append(_Token(_Tok.EOF))
    return toks


# ---------------------------------------------------------------------------
# Parser (recursive descent, precedence OR < AND < NOT)
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, toks: list[_Token]) -> None:
        self._toks = toks
        self._pos = 0

    def _peek(self) -> _Token:
        return self._toks[self._pos]

    def _advance(self) -> _Token:
        tok = self._toks[self._pos]
        self._pos += 1
        return tok

    def parse(self) -> Node | None:
        if self._peek().kind == _Tok.EOF:
            return None
        node = self._parse_or()
        if self._peek().kind != _Tok.EOF:
            raise ValueError("trailing tokens in match query")
        return node

    def _parse_or(self) -> Node:
        left = self._parse_and()
        while self._peek().kind == _Tok.OR:
            self._advance()
            left = Or(left, self._parse_and())
        return left

    def _parse_and(self) -> Node:
        left = self._parse_not()
        while self._peek().kind == _Tok.AND:
            self._advance()
            left = And(left, self._parse_not())
        return left

    def _parse_not(self) -> Node:
        if self._peek().kind == _Tok.NOT:
            self._advance()
            return Not(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        tok = self._peek()
        if tok.kind == _Tok.LPAREN:
            self._advance()
            node = self._parse_or()
            if self._peek().kind != _Tok.RPAREN:
                raise ValueError("expected ')' in match query")
            self._advance()
            return node
        if tok.kind == "STRING":
            self._advance()
            if self._peek().kind == _Tok.EQUAL:
                self._advance()
                val = self._peek()
                if val.kind != "STRING":
                    raise ValueError("expected value after '=' in match query")
                self._advance()
                return Equals(tok.value, val.value)
            return Key(tok.value)
        raise ValueError(f"unexpected token in match query: {tok.kind}")


class MatchQuery:
    """A parsed match query. ``None`` AST matches everything."""

    __slots__ = ("_node", "_source")

    def __init__(self, node: Node | None, source: str) -> None:
        self._node = node
        self._source = source

    @classmethod
    def of_string(cls, s: str) -> MatchQuery:
        node = _Parser(_tokenize(s)).parse()
        return cls(node, s)

    def to_string(self) -> str:
        return self._source

    def eval(self, ms: MatchSet) -> bool:
        if self._node is None:
            return True
        return _eval(self._node, ms)


def _eval(node: Node, ms: MatchSet) -> bool:
    if isinstance(node, And):
        return _eval(node.left, ms) and _eval(node.right, ms)
    if isinstance(node, Or):
        return _eval(node.left, ms) or _eval(node.right, ms)
    if isinstance(node, Not):
        return not _eval(node.inner, ms)
    if isinstance(node, Equals):
        return ms.contains(node.key, node.value)
    if isinstance(node, Key):
        return ms.find_by_key(node.key) is not None
    raise AssertionError(f"unknown node: {node!r}")  # pragma: no cover
