"""What ansible will accept as an inventory name, and what it will not (#1967).

Pure functions, so a plain unit test. The module used to carry Terrapod's own
merge -- union the hosts, union the memberships, let a later source win a
conflicting variable -- measured against ansible-core 2.18.3. Those measurements
were right and the implementation was still the wrong shape: ansible performs
the merge, the precedence, the group DAG and the `--limit` expansion itself, so
Terrapod renders the input and reads the output.

What stays is the part ansible will NOT do: refuse a name at the boundary,
before it is stored. Ansible is laxer and warns about the rest, but a name it
warns about cannot be used reliably in a `--limit` pattern or a `group_vars`
filename -- so the refusal belongs at the write, where it can name the offending
value, rather than at the point a playbook mysteriously targets nothing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from terrapod.services.inventory_resolution import (
    DERIVED_GROUPS,
    InventoryValidationError,
    validate_group_name,
    validate_host_name,
    validate_var_key,
)


class TestHostNames:
    """The forbidden set is not cosmetic: every character in it means something
    to `--limit`."""

    @pytest.mark.parametrize("name", ["web-01", "db.internal", "host_1", "a", "WEB01"])
    def test_an_ordinary_host_name_is_accepted(self, name):
        """Dots, hyphens and underscores are fine, which is what most real
        inventories are made of."""
        assert validate_host_name(name) == name

    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("web 1", "whitespace, which --limit splits on"),
            ("\tweb", "whitespace"),
            ("web\n", "whitespace"),
            ("web,db", "a separator"),
            ("a:b", "a separator"),
            ("!web", "the exclusion operator"),
            ("x&y", "the intersection operator"),
            ("~re", "the regex operator"),
        ],
    )
    def test_a_name_that_cannot_be_targeted_is_refused(self, name, why):
        with pytest.raises(InventoryValidationError):
            validate_host_name(name)

    def test_the_leading_bang_refusal_says_why_it_is_worse_than_unusable(self):
        """`!web` cannot be selected AND silently excludes `web` from any
        pattern naming it, which is the half that makes it dangerous rather
        than merely broken. The message has to carry that."""
        with pytest.raises(InventoryValidationError) as exc:
            validate_host_name("!web")
        assert "silently exclude" in str(exc.value)

    @pytest.mark.parametrize("name", ["", None, 123])
    def test_a_non_string_or_empty_name_is_refused(self, name):
        with pytest.raises(InventoryValidationError):
            validate_host_name(name)  # type: ignore[arg-type]


class TestGroupNames:
    @pytest.mark.parametrize("name", ["web", "_internal", "db2", "A", "web_prod"])
    def test_an_identifier_is_accepted(self, name):
        assert validate_group_name(name) == name

    @pytest.mark.parametrize("name", ["web-prod", "2web", "web.prod", "web prod", ""])
    def test_a_name_ansible_would_warn_about_is_refused(self, name):
        """Stricter than ansible on purpose: ansible stores these and warns, and
        a name it warns about cannot be used reliably in a `--limit` pattern or
        a `group_vars` filename."""
        with pytest.raises(InventoryValidationError):
            validate_group_name(name)

    @pytest.mark.parametrize("name", sorted(DERIVED_GROUPS))
    def test_a_derived_name_is_refused(self, name):
        with pytest.raises(InventoryValidationError) as exc:
            validate_group_name(name)
        assert "derived by ansible" in str(exc.value)

    def test_the_derived_refusal_points_at_where_global_variables_go(self):
        """`all` is the one an operator actually wants, and they usually want it
        for variables rather than for membership. Refusing without saying where
        those go would just move the dead end -- `group_vars/all` is a real
        ansible structure and Terrapod stores it as one.
        """
        with pytest.raises(InventoryValidationError) as exc:
            validate_group_name("all")
        message = str(exc.value)
        assert "group_vars/all" in message
        assert "on the inventory itself" in message

    def test_all_is_refused_because_the_document_is_rooted_at_it(self):
        """Pinned as its own case because the reason differs from `ungrouped`.

        `ungrouped` is merely derived. `all` is the rendered document's own root
        key, so a declared group of that name would collide with the structure
        rather than duplicate a computed one -- which is why the renderer can
        put inventory-wide variables at that root without ambiguity.
        """
        assert "all" in DERIVED_GROUPS
        assert "ungrouped" in DERIVED_GROUPS


class TestVariableNames:
    """Deliberately laxer than the group rule."""

    @pytest.mark.parametrize(
        "key", ["ansible_host", "odd-name", "with.dots", "UPPER", "2leading", "x"]
    )
    def test_anything_non_empty_is_accepted(self, key):
        """Ansible stores a variable whose name is not an identifier and only
        warns that it is unreachable as `{{ name }}` -- it is still readable via
        `hostvars['h']['odd-name']`, which some roles do on purpose. Refusing
        more would block working configurations to prevent a warning.
        """
        assert validate_var_key(key) == key

    @pytest.mark.parametrize("key", ["", None, 42, " "])
    def test_an_empty_or_non_string_key_is_refused(self, key):
        with pytest.raises(InventoryValidationError):
            validate_var_key(key)  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", [" ansible_host", "ansible_host ", "\tx"])
    def test_surrounding_whitespace_is_refused(self, key):
        """Almost certainly a mistake, and invisible in every surface that
        displays it -- so the one place to catch it is the write."""
        with pytest.raises(InventoryValidationError) as exc:
            validate_var_key(key)
        assert "whitespace" in str(exc.value)


class TestTerrapodImplementsNoMergeOfItsOwn:
    """Derived from the source, because this is the decision most likely to be
    quietly undone.

    Ansible performs the merge, the precedence, the group DAG, the derivation of
    `all` and `ungrouped`, and the expansion of `--limit`. A reimplementation
    here would be a second answer to every one of those questions, and the two
    would diverge the first time ansible changed. The measurements that were
    taken to write the old version are the reason to trust ansible's, not a
    reason to keep ours.
    """

    def _source(self) -> str:
        here = pathlib.Path(__file__).resolve()
        for root in (here.parents[2], here.parents[1]):
            candidate = root / "terrapod" / "services" / "inventory_resolution.py"
            if candidate.is_file():
                return candidate.read_text()
        raise AssertionError("inventory_resolution.py not found under either layout")

    @pytest.mark.parametrize(
        "name", ["def merge(", "def to_ansible_inventory(", "def limit_matches("]
    )
    def test_the_retired_merge_functions_are_not_back(self, name):
        assert name not in self._source(), (
            f"{name} is back. Ansible performs the merge, the precedence, the group DAG "
            f"and the --limit expansion; a second implementation here is a second answer "
            f"that will diverge from it."
        )

    def test_the_module_touches_no_database_network_or_subprocess(self):
        """It is the pure half on purpose, so it can be tested without standing
        anything up -- and so the ansible invocation has exactly one home."""
        source = self._source()
        for forbidden in ("import subprocess", "AsyncSession", "httpx", "ansible-inventory"):
            assert forbidden not in source, f"{forbidden} does not belong in the pure half"

    def test_it_declares_only_the_validators_and_their_constants(self):
        """A floor, so a renamed module cannot pass by exporting nothing, and a
        ceiling, so the merge cannot creep back under a new name."""
        source = self._source()
        functions = set(re.findall(r"^def (\w+)\(", source, re.M))
        assert functions == {"validate_group_name", "validate_host_name", "validate_var_key"}, (
            f"unexpected public surface: {sorted(functions)}"
        )
