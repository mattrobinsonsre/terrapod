"""A tiny boolean query language for selecting usage-catalogue entries.

Each usage entry carries a query that decides whether it applies to a given
product's combined match set. The language is deliberively small — equality and
key-existence tests joined by ``and`` / ``or`` / ``not`` with parentheses::

    expr := or
    or   := and ( 'or'  and )*
    and  := not ( 'and' not )*
    not  := 'not' not | atom
    atom := '(' expr ')' | WORD '=' WORD | WORD

Precedence is ``or`` < ``and`` < ``not``. A ``WORD`` is either a bare token (any
run of characters other than whitespace and ``'"()=``) or a quoted ``"…"`` /
``'…'`` string with ``\\`` escapes. A bare word is a *key-existence* test; ``k =
v`` is an *equality* test. Evaluated against a
:class:`~terrapod.services.cost.match_set.MatchSet`, ``k = v`` holds when that
pair is present and ``k`` holds when the key is present with any value. The empty
query matches everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from terrapod.services.cost.match_set import MatchSet

# --- AST ---------------------------------------------------------------------
# Each node knows how to evaluate itself against a match set, so evaluation is a
# straight recursion over the tree rather than a central dispatch.


@dataclass(frozen=True)
class And:
    left: Node
    right: Node

    def eval(self, ms: MatchSet) -> bool:
        return self.left.eval(ms) and self.right.eval(ms)


@dataclass(frozen=True)
class Or:
    left: Node
    right: Node

    def eval(self, ms: MatchSet) -> bool:
        return self.left.eval(ms) or self.right.eval(ms)


@dataclass(frozen=True)
class Not:
    inner: Node

    def eval(self, ms: MatchSet) -> bool:
        return not self.inner.eval(ms)


@dataclass(frozen=True)
class Equals:
    key: str
    value: str

    def eval(self, ms: MatchSet) -> bool:
        return ms.contains(self.key, self.value)


@dataclass(frozen=True)
class Key:
    key: str

    def eval(self, ms: MatchSet) -> bool:
        return ms.find_by_key(self.key) is not None


Node = And | Or | Not | Equals | Key


# --- lexer -------------------------------------------------------------------

_SPECIAL = set(" '\"()=")


@dataclass(frozen=True)
class _Token:
    kind: str  # "(", ")", "=", "and", "or", "not", "STRING", or "EOF"
    value: str = ""


_EOF = _Token("EOF")


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c in "()=":
            tokens.append(_Token(c))
            i += 1
        elif c in "'\"":
            quote = c
            i += 1
            buf: list[str] = []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise ValueError(f"unterminated string in match query: {text!r}")
            i += 1  # closing quote
            tokens.append(_Token("STRING", "".join(buf)))
        else:
            start = i
            while i < n and text[i] not in _SPECIAL:
                i += 1
            word = text[start:i]
            tokens.append(_Token(word) if word in ("and", "or", "not") else _Token("STRING", word))
    tokens.append(_EOF)
    return tokens


# --- parser (recursive descent, one method per precedence level) -------------


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _take(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: str) -> None:
        if self._peek().kind != kind:
            raise ValueError(f"expected {kind!r} in match query")
        self._pos += 1

    def parse(self) -> Node | None:
        if self._peek().kind == "EOF":
            return None
        node = self._disjunction()
        if self._peek().kind != "EOF":
            raise ValueError("trailing tokens in match query")
        return node

    def _disjunction(self) -> Node:
        node = self._conjunction()
        while self._peek().kind == "or":
            self._take()
            node = Or(node, self._conjunction())
        return node

    def _conjunction(self) -> Node:
        node = self._negation()
        while self._peek().kind == "and":
            self._take()
            node = And(node, self._negation())
        return node

    def _negation(self) -> Node:
        if self._peek().kind == "not":
            self._take()
            return Not(self._negation())
        return self._term()

    def _term(self) -> Node:
        tok = self._peek()
        if tok.kind == "(":
            self._take()
            node = self._disjunction()
            self._expect(")")
            return node
        if tok.kind == "STRING":
            self._take()
            if self._peek().kind == "=":
                self._take()
                value = self._peek()
                if value.kind != "STRING":
                    raise ValueError("expected value after '=' in match query")
                self._take()
                return Equals(tok.value, value.value)
            return Key(tok.value)
        raise ValueError(f"unexpected token in match query: {tok.kind}")


class MatchQuery:
    """A parsed query. A ``None`` tree (the empty query) matches everything."""

    __slots__ = ("_source", "_tree")

    def __init__(self, tree: Node | None, source: str) -> None:
        self._tree = tree
        self._source = source

    @classmethod
    def parse(cls, text: str) -> MatchQuery:
        return cls(_Parser(_tokenize(text)).parse(), text)

    def to_string(self) -> str:
        return self._source

    def eval(self, ms: MatchSet) -> bool:
        return self._tree is None or self._tree.eval(ms)
