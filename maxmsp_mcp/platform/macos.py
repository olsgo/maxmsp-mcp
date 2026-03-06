from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..json_utils import parse_json_object_text
from ..process_utils import run_command


def supported() -> bool:
    if os.name != "posix":
        return False
    try:
        return "darwin" in os.uname().sysname.lower()
    except Exception:
        return False


def open_path_in_app(app_path: Path, target: Path, *, bring_to_front: bool) -> dict:
    args = ["open"]
    if bring_to_front:
        args.extend(["-a", str(app_path), str(target)])
    else:
        args.extend(["-g", "-a", str(app_path), str(target)])
    subprocess.run(
        args,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"success": True, "path": str(target), "bring_to_front": bool(bring_to_front)}


def launch_app_with_document(app_path: Path, target: Path) -> dict:
    subprocess.run(
        ["open", "-a", str(app_path), str(target)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"launched": True}


def run_jxa(script: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["osascript", "-l", "JavaScript", "-e", script],
        timeout=timeout,
    )


def scan_open_documents(*, timeout: float = 3.0) -> dict:
    if not supported():
        return {
            "available": False,
            "method": "osascript_jxa",
            "reason": "unsupported_platform",
            "failure_kind": "unsupported_platform",
            "timeout_seconds": timeout,
            "documents": [],
        }

    script = (
        "const app = Application('Max');"
        "app.includeStandardAdditions = true;"
        "let docs = [];"
        "try {"
        "  docs = app.documents().map(function(d) {"
        "    let name = '';"
        "    let path = '';"
        "    try { name = d.name(); } catch (e) {}"
        "    try { const f = d.file(); if (f) { path = f.toString(); } } catch (e) {}"
        "    return {name: String(name || ''), path: String(path || '')};"
        "  });"
        "} catch (e) { docs = []; }"
        "JSON.stringify({documents: docs});"
    )
    try:
        out = run_jxa(script, timeout=timeout)
    except Exception as exc:
        reason = f"osascript_error:{exc}"
        failure_kind = "timeout" if "timed out" in reason.lower() else "osascript_error"
        return {
            "available": False,
            "method": "osascript_jxa",
            "reason": reason,
            "failure_kind": failure_kind,
            "timeout_seconds": timeout,
            "documents": [],
        }

    if out.returncode != 0:
        reason = (out.stderr or "").strip() or f"osascript_exit_{out.returncode}"
        failure_kind = "timeout" if "timed out" in reason.lower() else "osascript_error"
        return {
            "available": False,
            "method": "osascript_jxa",
            "reason": reason,
            "failure_kind": failure_kind,
            "timeout_seconds": timeout,
            "documents": [],
        }

    payload, parse_error = parse_json_object_text(out.stdout or "")
    if payload is None:
        return {
            "available": False,
            "method": "osascript_jxa",
            "reason": f"json_decode_error:{parse_error}",
            "failure_kind": "decode_error",
            "timeout_seconds": timeout,
            "documents": [],
        }

    documents = payload.get("documents")
    if not isinstance(documents, list):
        documents = []
    return {
        "available": True,
        "method": "osascript_jxa",
        "reason": None,
        "failure_kind": None,
        "timeout_seconds": timeout,
        "documents": documents,
    }


def close_document(target: Path, *, timeout: float = 5.0) -> dict:
    script = (
        "const app = Application('Max');"
        "const target = " + json.dumps(str(target)) + ";"
        "let closed = 0;"
        "let errors = 0;"
        "try {"
        "  const docs = app.documents();"
        "  for (let i = 0; i < docs.length; i++) {"
        "    const d = docs[i];"
        "    let p = '';"
        "    try { const f = d.file(); if (f) { p = String(f.toString()); } } catch (e) {}"
        "    if (p === target) {"
        "      try { d.close({ saving: 'no' }); closed++; } catch (e) { errors++; }"
        "    }"
        "  }"
        "} catch (e) { errors++; }"
        "JSON.stringify({closed: closed, errors: errors, path: target});"
    )
    out = run_jxa(script, timeout=timeout)
    if out.returncode != 0:
        return {
            "success": False,
            "error": (out.stderr or "").strip() or "Failed to close patch window.",
        }
    payload, parse_error = parse_json_object_text(out.stdout or "")
    if payload is None:
        payload = {"raw": (out.stdout or "").strip(), "parse_error": parse_error}
    return {"success": True, "result": payload}
