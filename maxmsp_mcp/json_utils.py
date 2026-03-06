from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def compact_json(payload: Any, *, ensure_ascii: bool = False) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=ensure_ascii)


def compact_json_size(payload: Any, *, ensure_ascii: bool = False) -> int:
    return len(compact_json(payload, ensure_ascii=ensure_ascii))


def canonical_json(payload: Any, *, ensure_ascii: bool = True) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii)


def pretty_json(
    payload: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
) -> str:
    return json.dumps(
        payload,
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
    )


def read_json_file(path: Path, *, encoding: str = "utf-8") -> Any:
    return json.loads(path.read_text(encoding=encoding))


def read_json_object_file(
    path: Path,
    *,
    encoding: str = "utf-8",
    default: dict | None = None,
) -> dict:
    fallback = {} if default is None else dict(default)
    try:
        payload = read_json_file(path, encoding=encoding)
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    return payload


def write_json_file(
    path: Path,
    payload: Any,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
) -> None:
    path.write_text(
        pretty_json(
            payload,
            indent=indent,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
        ),
        encoding=encoding,
    )


def parse_json_text(raw: Any) -> tuple[Any | None, str | None]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8").strip()
        except Exception as exc:
            return None, str(exc)
    else:
        text = str(raw or "").strip()
    if not text:
        return None, "empty output"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def parse_json_object_text(raw: Any) -> tuple[dict | None, str | None]:
    parsed, error = parse_json_text(raw)
    if error is not None:
        return None, error
    if not isinstance(parsed, dict):
        return None, "JSON payload is not an object"
    return parsed, None
