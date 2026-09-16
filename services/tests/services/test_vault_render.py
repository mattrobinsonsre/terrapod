"""The Vault file renderer (#1648): templates, formats and base64.

Pure-function tests against `terrapod.services.vault_render`. The properties:

- the goldens — an AWS credentials file, a kubeconfig and a PEM bundle — render
  exactly, byte for byte;
- a value is never re-scanned, so a secret containing `{{` lands verbatim;
- an unknown name or filter fails with a message naming the key, never a value;
- base64 decodes to UTF-8 text or refuses clearly;
- `format` json/env write what they document, and `env` escaping round-trips
  through a real POSIX shell.
"""

import base64
import json
import shutil
import subprocess

import pytest

from terrapod.services.vault_render import (
    FILTERS,
    LEASE_KEYS,
    MAX_TEMPLATE_BYTES,
    RenderError,
    decode_base64_text,
    env_escape,
    parse_template,
    render_format,
    render_template,
)

SECRET = "S3CR3T-render-value-must-not-leak"

# ── Goldens ───────────────────────────────────────────────────────────


AWS_CREDS = {"access_key": "AKIAEXAMPLE", "secret_key": "wJalr/EXAMPLEKEY", "lease_id": "x"}


def test_golden_aws_credentials_ini():
    template = (
        "[default]\n"
        "aws_access_key_id = {{ access_key }}\n"
        "aws_secret_access_key = {{ secret_key }}\n"
    )
    assert render_template(template, AWS_CREDS) == (
        "[default]\naws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = wJalr/EXAMPLEKEY\n"
    )


def test_golden_aws_sts_with_session_token_and_lease_expiry():
    data = {"access_key": "ASIAEXAMPLE", "secret_key": "sk", "security_token": "tok"}
    lease = {"ttl": 3600, "renewable": False, "expires_at": "2026-09-15T13:00:00Z"}
    template = (
        "[default]\n"
        "aws_access_key_id={{access_key}}\n"
        "aws_secret_access_key={{secret_key}}\n"
        "aws_session_token={{security_token}}\n"
        "# expires {{ _lease.expires_at }} (ttl {{ _lease.ttl }}s, renewable {{ _lease.renewable }})\n"
    )
    assert render_template(template, data, lease) == (
        "[default]\n"
        "aws_access_key_id=ASIAEXAMPLE\n"
        "aws_secret_access_key=sk\n"
        "aws_session_token=tok\n"
        "# expires 2026-09-15T13:00:00Z (ttl 3600s, renewable false)\n"
    )


def test_golden_kubeconfig_with_indent_and_nested_lookup():
    data = {
        "cluster": {"server": "https://k8s.example.test:6443", "ca_data": "Q0EtREFUQQ=="},
        "token": "eyJhbGciOi.example",
        "namespace": "apps",
    }
    template = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "  - name: target\n"
        "    cluster:\n"
        "      server: {{ cluster.server }}\n"
        "      certificate-authority-data: {{ cluster.ca_data }}\n"
        "users:\n"
        "  - name: terrapod\n"
        "    user:\n"
        "      token: {{ token | trim }}\n"
        "contexts:\n"
        "  - name: target\n"
        "    context: {cluster: target, user: terrapod, namespace: {{ namespace }}}\n"
        "current-context: target\n"
    )
    assert render_template(template, data) == (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "  - name: target\n"
        "    cluster:\n"
        "      server: https://k8s.example.test:6443\n"
        "      certificate-authority-data: Q0EtREFUQQ==\n"
        "users:\n"
        "  - name: terrapod\n"
        "    user:\n"
        "      token: eyJhbGciOi.example\n"
        "contexts:\n"
        "  - name: target\n"
        "    context: {cluster: target, user: terrapod, namespace: apps}\n"
        "current-context: target\n"
    )


PEM_CERT = "-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----"
PEM_KEY = "-----BEGIN RSA PRIVATE KEY-----\nKEY\n-----END RSA PRIVATE KEY-----"
PEM_CA = [
    "-----BEGIN CERTIFICATE-----\nINTERMEDIATE\n-----END CERTIFICATE-----",
    "-----BEGIN CERTIFICATE-----\nROOT\n-----END CERTIFICATE-----",
]


def test_golden_pem_bundle_from_one_issue():
    data = {"certificate": PEM_CERT, "private_key": PEM_KEY, "ca_chain": PEM_CA, "serial": "1"}
    out = render_template("{{certificate}}\n{{private_key}}\n{{ca_chain|lines}}\n", data)
    assert out == (
        "-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----\n"
        "-----BEGIN RSA PRIVATE KEY-----\nKEY\n-----END RSA PRIVATE KEY-----\n"
        "-----BEGIN CERTIFICATE-----\nINTERMEDIATE\n-----END CERTIFICATE-----\n"
        "-----BEGIN CERTIFICATE-----\nROOT\n-----END CERTIFICATE-----\n"
    )


def test_indent_pads_every_line_after_the_first_for_a_yaml_block():
    out = render_template("key: |\n  {{ pem | indent 2 }}\n", {"pem": "a\nb\n\nc"})
    assert out == "key: |\n  a\n  b\n\n  c\n"


# ── Values are never re-scanned ────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "{{ secret_key }}",
        "{{secret_key}} and {{ _lease.ttl }}",
        "{{ unclosed",
        "}} {{",
        "{{ nope | json }}",
    ],
)
def test_a_value_containing_template_syntax_lands_verbatim(value):
    data = {"a": value, "secret_key": SECRET}
    out = render_template("x={{ a }};", data, {"ttl": 1, "renewable": True, "expires_at": "z"})
    assert out == f"x={value};"
    assert SECRET not in out


def test_one_value_cannot_reach_into_the_next_tag():
    data = {"a": "{{", "b": "secret_key }}", "secret_key": SECRET}
    assert render_template("{{a}}{{b}}", data) == "{{secret_key }}"


# ── Names and filters ──────────────────────────────────────────────────


def test_non_string_values_render_as_json():
    data = {"n": 5, "t": True, "z": None, "m": {"k": "v"}, "l": ["a", 1]}
    assert render_template("{{n}} {{t}} {{z}} {{m}} {{l}}", data) == (
        '5 true null {"k": "v"} ["a", 1]'
    )


def test_json_filter_quotes_a_string_and_serialises_a_map():
    data = {"s": 'he said "hi"\n', "m": {"a": [1, 2]}}
    assert render_template("{{ s | json }} {{ m | json }}", data) == (
        '"he said \\"hi\\"\\n" {"a": [1, 2]}'
    )


def test_filters_apply_left_to_right():
    enc = base64.b64encode(b"  line1\nline2  ").decode()
    assert render_template("{{ v | base64decode | trim | indent 3 }}", {"v": enc}) == (
        "line1\n   line2"
    )


def test_an_unknown_name_fails_naming_it_and_what_is_there_never_a_value():
    with pytest.raises(RenderError) as e:
        render_template("{{ nope }}", {"access_key": SECRET, "secret_key": SECRET})
    msg = str(e.value)
    assert msg == (
        "tag {{ nope }} names 'nope', which is not in the secret "
        "(available: access_key, secret_key)"
    )
    assert SECRET not in msg


def test_an_unknown_nested_name_names_the_map_it_looked_in():
    with pytest.raises(RenderError) as e:
        render_template("{{ creds.nope }}", {"creds": {"key": SECRET}})
    assert str(e.value) == (
        "tag {{ creds.nope }} names 'creds.nope', which is not in 'creds' (available: key)"
    )
    assert SECRET not in str(e.value)


def test_reaching_into_a_string_says_it_is_not_a_map():
    with pytest.raises(RenderError) as e:
        render_template("{{ token.x }}", {"token": SECRET})
    assert str(e.value) == "tag {{ token.x }}: 'token' is not a map, so it has no 'x'"


def test_an_unknown_filter_is_refused_at_parse_time_listing_the_filters():
    with pytest.raises(RenderError) as e:
        parse_template("{{ a | upper }}")
    assert str(e.value) == (
        f"tag {{{{ a | upper }}}} uses unknown filter 'upper' (available: {', '.join(FILTERS)})"
    )


@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("{{ }}", "names nothing"),
        ("{{ a b }}", "has an invalid name 'a b'"),
        ("{{ a..b }}", "has an invalid name 'a..b'"),
        ("{{ a | }}", "has an empty filter after '|'"),
        ("{{ a | indent }}", "filter 'indent' takes one whole number"),
        ("{{ a | indent x }}", "filter 'indent' takes one whole number"),
        ("{{ a | indent -1 }}", "filter 'indent' takes one whole number"),
        ("{{ a | indent 65 }}", "filter 'indent' is limited to 64 spaces"),
        ("{{ a | trim 2 }}", "filter 'trim' takes no argument"),
        ("{{ _lease }}", "_lease offers _lease.ttl, _lease.renewable, _lease.expires_at"),
        ("{{ _lease.lease_id }}", "_lease offers"),
        ("{{ _lease.ttl.x }}", "_lease offers"),
        ("text {{ unclosed", "has a '{{' with no closing '}}'"),
        ("{{ a }} then {{ b", "has a '{{' with no closing '}}'"),
        ("{{ a {{ b }}", "has an invalid name"),
    ],
)
def test_syntax_errors_are_caught_without_any_data(template, reason):
    with pytest.raises(RenderError) as e:
        parse_template(template)
    assert reason in str(e.value)


def test_a_lone_closing_brace_pair_is_ordinary_text():
    assert render_template('{"a": {"b": "{{ v }}"}}', {"v": "x"}) == '{"a": {"b": "x"}}'


def test_the_lease_id_is_not_offered():
    assert "lease_id" not in LEASE_KEYS


def test_a_lease_tag_without_a_lease_fails_clearly():
    with pytest.raises(RenderError) as e:
        render_template("{{ _lease.ttl }}", {"a": "b"}, None)
    assert "carries no lease" in str(e.value)


def test_lines_needs_a_list():
    with pytest.raises(RenderError, match="filter 'lines' needs a list"):
        render_template("{{ a | lines }}", {"a": SECRET})


def test_the_template_size_cap_is_in_bytes():
    ok = "é" * (MAX_TEMPLATE_BYTES // 2)
    parse_template(ok)
    with pytest.raises(RenderError, match="over the 16 KiB limit for a template"):
        parse_template(ok + "x")


def test_a_non_string_template_is_refused():
    with pytest.raises(RenderError, match="must be a string"):
        parse_template(["{{ a }}"])


# ── base64 ─────────────────────────────────────────────────────────────


def test_base64_decodes_to_utf8_text_and_ignores_wrapping():
    raw = '{"type": "service_account", "name": "café"}'
    enc = base64.b64encode(raw.encode()).decode()
    wrapped = "\n".join(enc[i : i + 8] for i in range(0, len(enc), 8))
    assert decode_base64_text(wrapped, what="x") == raw
    assert render_template("{{ k | base64decode }}", {"k": enc}) == raw


def test_base64_that_is_not_utf8_is_refused_as_binary():
    enc = base64.b64encode(b"\xff\xfe\x00binary").decode()
    with pytest.raises(RenderError) as e:
        render_template("{{ k | base64decode }}", {"k": enc})
    assert "not UTF-8 text; binary files are not supported yet" in str(e.value)
    assert enc not in str(e.value)


def test_invalid_base64_is_refused_without_echoing_it():
    with pytest.raises(RenderError) as e:
        decode_base64_text(SECRET + "!!", what="field 'k'")
    assert str(e.value) == "field 'k' is not valid base64"


def test_base64_of_a_map_is_refused():
    with pytest.raises(RenderError, match="is not a string, so it cannot be base64-decoded"):
        render_template("{{ m | base64decode }}", {"m": {"a": 1}})


# ── format ─────────────────────────────────────────────────────────────


def test_format_json_writes_the_whole_map_indented_with_a_trailing_newline():
    data = {"b": 1, "a": {"x": "y"}, "c": "é"}
    out = render_format("json", data)
    assert out == '{\n  "b": 1,\n  "a": {\n    "x": "y"\n  },\n  "c": "é"\n}\n'
    assert json.loads(out) == data


def test_format_json_fields_selects_a_subset_in_the_given_order():
    data = {"a": 1, "b": 2, "c": 3}
    assert json.loads(render_format("json", data, ["c", "a"])) == {"c": 3, "a": 1}
    assert list(json.loads(render_format("json", data, ["c", "a"]))) == ["c", "a"]


def test_format_fields_missing_from_the_secret_are_named():
    with pytest.raises(RenderError) as e:
        render_format("json", {"a": SECRET}, ["a", "b", "c"])
    assert str(e.value) == "fields 'b', 'c' are not in the secret (available: a)"


def test_format_env_writes_quoted_lines():
    data = {"USER": "app", "PORT": 5432, "TLS": True}
    assert render_format("env", data) == 'USER="app"\nPORT="5432"\nTLS="true"\n'


def test_env_escaping_is_exactly_the_four_shell_specials():
    assert env_escape("a\\b\"c$d`e\nf\tg'h") == "a\\\\b\\\"c\\$d\\`e\nf\tg'h"


@pytest.mark.parametrize("key", ["1BAD", "has-dash", "sp ace", ""])
def test_format_env_refuses_a_key_that_is_not_an_env_name(key):
    with pytest.raises(RenderError) as e:
        render_format("env", {key: SECRET})
    assert f"{key!r} is not" in str(e.value)
    assert SECRET not in str(e.value)


def test_format_env_refuses_a_nul():
    with pytest.raises(RenderError, match="the value of 'K' contains a NUL byte"):
        render_format("env", {"K": "a\x00b"})


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_format_env_round_trips_through_a_real_posix_shell(tmp_path):
    """The definition the docs give: sourcing the file yields the value exactly."""
    tricky = {
        "DOLLAR": "pa$$word $HOME ${HOME}",
        "QUOTES": 'it\'s "quoted"',
        "BACKSLASH": "C:\\path\\n\\",
        "BACKTICK": "`id` $(id)",
        "MULTILINE": "-----BEGIN KEY-----\nabc\n\n-----END KEY-----\n",
        "UNICODE": "café ☕",
        "EMPTY": "",
    }
    f = tmp_path / "vars.env"
    f.write_text(render_format("env", tricky))
    script = "set -a; . ./vars.env; set +a; " + "; ".join(f'printf "%s\\0" "${k}"' for k in tricky)
    out = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, check=True, env={}
    ).stdout.decode()
    assert out.split("\0")[:-1] == list(tricky.values())


def test_an_unknown_format_is_refused():
    with pytest.raises(RenderError, match="unknown format 'ini'"):
        render_format("ini", {})
