"""Extract Terraform variable and output declarations from module tarballs."""

import io
import json
import re
import tarfile

import hcl2

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# The longest reason stored on a module version (#1707). It is shown in the UI
# next to the version, so it is a short summary, never a dump.
MAX_INTERFACE_ERROR_LENGTH = 500

_ARCHIVE_UNREADABLE = "The module archive could not be read as a gzip-compressed tar file."
_INTERFACE_UNREADABLE = "The module interface could not be read."


def extract_module_interface(tarball_bytes: bytes) -> dict:
    """Parse .tf files from a module tarball (in-memory) and extract blocks.

    Returns {"inputs": [...], "outputs": [...]}.
    Returns {"inputs": [], "outputs": []} on parse failure.

    Prefer `extract_module_interface_from_file` for uploads — it streams the
    tarball from disk rather than holding the whole archive in the heap.
    Callers that store the interface use `extract_module_interface_result`,
    which also says whether parsing failed.
    """
    result = extract_module_interface_result(tarball_bytes)
    return {"inputs": result["inputs"], "outputs": result["outputs"]}


def extract_module_interface_from_file(tarball_path: str) -> dict:
    """Parse .tf files from a module tarball on disk and extract blocks.

    Opens the tarball by path so `tarfile` streams members from disk — the
    whole archive is never loaded into the worker heap (CLAUDE.md #14).
    Returns {"inputs": [...], "outputs": [...]}; {} halves on parse failure.
    """
    result = extract_module_interface_result_from_file(tarball_path)
    return {"inputs": result["inputs"], "outputs": result["outputs"]}


def extract_module_interface_result(tarball_bytes: bytes) -> dict:
    """Like `extract_module_interface`, and also report failure (#1707).

    Returns {"inputs": [...], "outputs": [...], "error": str | None}. `error`
    is None when every root `.tf` file was read and parsed. Otherwise it is a
    short reason, safe to show a user: no traceback, no file-system path,
    bounded length. Declarations from the files that did parse are still
    returned, so the interface may be partial when `error` is set.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            return _interface_from_tar(tar)
    except (tarfile.TarError, OSError, EOFError):
        logger.warning("Failed to open module tarball for interface extraction", exc_info=True)
        return _failed(_ARCHIVE_UNREADABLE)
    except Exception:
        logger.warning("Failed to extract module interface", exc_info=True)
        return _failed(_INTERFACE_UNREADABLE)


def extract_module_interface_result_from_file(tarball_path: str) -> dict:
    """Like `extract_module_interface_from_file`, and also report failure.

    Same return shape as `extract_module_interface_result`.
    """
    try:
        with tarfile.open(tarball_path, mode="r:gz") as tar:
            return _interface_from_tar(tar)
    except (tarfile.TarError, OSError, EOFError):
        logger.warning("Failed to open module tarball for interface extraction", exc_info=True)
        return _failed(_ARCHIVE_UNREADABLE)
    except Exception:
        logger.warning("Failed to extract module interface", exc_info=True)
        return _failed(_INTERFACE_UNREADABLE)


def _failed(reason: str) -> dict:
    return {"inputs": [], "outputs": [], "error": reason}


def _interface_from_tar(tar: tarfile.TarFile) -> dict:
    """Extract inputs/outputs from an open module tarball."""
    inputs: list[dict] = []
    outputs: list[dict] = []
    files, problems = _read_root_tf_files(tar)
    for file_name, content in files:
        parsed, problem = _parse_hcl(content)
        if parsed is None:
            problems.append(f"{_safe_file_name(file_name)}: {problem}")
            continue
        inputs.extend(_extract_variables(parsed))
        outputs.extend(_extract_outputs(parsed))
    return {"inputs": inputs, "outputs": outputs, "error": _summarise(problems)}


def _summarise(problems: list[str]) -> str | None:
    if not problems:
        return None
    reason = "; ".join(problems)
    if len(reason) > MAX_INTERFACE_ERROR_LENGTH:
        reason = reason[: MAX_INTERFACE_ERROR_LENGTH - 3].rstrip() + "..."
    return reason


_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_file_name(name: str) -> str:
    """A root-level member name, reduced to characters safe to display."""
    cleaned = _UNSAFE_NAME_CHARS.sub("_", name)
    return cleaned[:100] or "(unnamed)"


_MAX_TF_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per file


def _read_root_tf_files(tar: tarfile.TarFile) -> tuple[list[tuple[str, str]], list[str]]:
    """Read all .tf files at the root level of an open tarball.

    Returns ([(member name, content)], [problem]). A root `.tf` file skipped
    because it cannot be read is reported as a problem, not silently dropped.
    """
    contents: list[tuple[str, str]] = []
    problems: list[str] = []
    for member in tar.getmembers():
        if not member.isfile():
            continue
        # `tar -czf m.tgz -C dir .` -- the documented invocation -- names every
        # entry `./main.tf`. Without normalising, the root check below read
        # those as nested and the interface parsed as empty (#1707).
        name = member.name
        while name.startswith("./"):
            name = name[2:]
        if "/" in name:
            continue
        if not name.endswith(".tf"):
            continue
        if member.size > _MAX_TF_FILE_BYTES:
            logger.warning(
                "Skipping oversized .tf file",
                file=member.name,
                size=member.size,
            )
            problems.append(f"{_safe_file_name(name)}: skipped, larger than 5 MB")
            continue
        f = tar.extractfile(member)
        if f is None:
            continue
        # The normalised name, so a reason reads `main.tf`, not `__main.tf`.
        contents.append((name, f.read().decode("utf-8", errors="replace")))
    return contents, problems


def _parse_hcl(content: str) -> tuple[dict | None, str]:
    """Parse HCL content. Returns (parsed, "") or (None, a short reason).

    The reason carries a position only: the parser's own message embeds its
    grammar and the offending source text, neither of which belongs in the UI.
    """
    try:
        return hcl2.loads(content), ""
    except Exception as exc:
        line = getattr(exc, "line", None)
        column = getattr(exc, "column", None)
        if isinstance(line, int) and isinstance(column, int) and line > 0:
            return None, f"invalid HCL at line {line}, column {column}"
        return None, "invalid HCL"


def _serialize_default(value) -> str | None:
    """Serialize a default value to a JSON-friendly string representation."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _normalize_type_expr(type_val) -> str:
    """Normalize a python-hcl2 type value to a clean type expression string.

    python-hcl2 returns types in varied forms:
      - "string" (simple primitives)
      - "${map(string)}" (interpolation-wrapped complex types)
      - ["map", "string"] (list form in some versions)
    This normalizes all forms to e.g. "string", "map(string)", "list(number)".
    """
    if type_val is None:
        return "any"
    if isinstance(type_val, str):
        if type_val.startswith("${") and type_val.endswith("}"):
            return type_val[2:-1]
        return type_val
    if isinstance(type_val, list) and len(type_val) > 0:
        return str(type_val[0]) if len(type_val) == 1 else str(type_val)
    return str(type_val)


def _type_expr_to_json_schema(expr: str) -> dict:
    """Convert a normalized type expression to JSON Schema."""
    expr = expr.strip()

    if expr == "string":
        return {"type": "string"}
    if expr == "number":
        return {"type": "number"}
    if expr == "bool":
        return {"type": "boolean"}
    if expr == "any":
        return {}

    if expr.startswith("map(") and expr.endswith(")"):
        return {"type": "object", "additionalProperties": _type_expr_to_json_schema(expr[4:-1])}

    if expr.startswith("list(") and expr.endswith(")"):
        return {"type": "array", "items": _type_expr_to_json_schema(expr[5:-1])}

    if expr.startswith("set(") and expr.endswith(")"):
        return {
            "type": "array",
            "uniqueItems": True,
            "items": _type_expr_to_json_schema(expr[4:-1]),
        }

    if expr.startswith("tuple(") and expr.endswith(")"):
        return {"type": "array"}

    if expr.startswith("object(") and expr.endswith(")"):
        return _parse_object_schema(expr[7:-1].strip())

    return {}


def _parse_object_schema(inner: str) -> dict:
    """Parse an object type's inner block into JSON Schema properties."""
    inner = inner.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()

    if not inner:
        return {"type": "object"}

    properties = {}
    for field in _split_object_fields(inner):
        field = field.strip()
        if "=" not in field:
            continue
        key, val = field.split("=", 1)
        properties[key.strip()] = _type_expr_to_json_schema(val.strip())

    return {"type": "object", "properties": properties, "required": list(properties.keys())}


def _split_object_fields(inner: str) -> list[str]:
    """Split object fields respecting nested parentheses/braces."""
    fields = []
    depth = 0
    current = ""
    for ch in inner:
        if ch in ("(", "{", "["):
            depth += 1
            current += ch
        elif ch in (")", "}", "]"):
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            fields.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        fields.append(current)
    return fields


def _extract_variables(parsed: dict) -> list[dict]:
    """Extract variable blocks from parsed HCL."""
    variables = []
    for var_block in parsed.get("variable", []):
        for var_name, var_config in var_block.items():
            has_default = "default" in var_config
            type_val = var_config.get("type")
            type_expr = _normalize_type_expr(type_val)
            variables.append(
                {
                    "name": var_name,
                    "type": type_expr,
                    "type_schema": _type_expr_to_json_schema(type_expr),
                    "description": var_config.get("description", ""),
                    "default": _serialize_default(var_config.get("default"))
                    if has_default
                    else None,
                    "required": not has_default,
                    "sensitive": bool(var_config.get("sensitive", False)),
                }
            )
    return variables


def _extract_outputs(parsed: dict) -> list[dict]:
    """Extract output blocks from parsed HCL."""
    outputs = []
    for out_block in parsed.get("output", []):
        for out_name, out_config in out_block.items():
            outputs.append(
                {
                    "name": out_name,
                    "description": out_config.get("description", ""),
                    "sensitive": bool(out_config.get("sensitive", False)),
                }
            )
    return outputs
