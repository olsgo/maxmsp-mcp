from __future__ import annotations

import math
import re
from typing import Any


QA_SEVERITY_WEIGHTS = {
    "critical": 20,
    "high": 12,
    "medium": 6,
    "low": 3,
}


def _is_int_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer()
    return False


def _extract_topology_rows(topology: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(topology, dict):
        return [], []
    boxes = topology.get("boxes", [])
    lines = topology.get("lines", [])
    if not isinstance(boxes, list):
        boxes = []
    if not isinstance(lines, list):
        lines = []
    normalized_boxes = [row for row in boxes if isinstance(row, dict)]
    normalized_lines = [row for row in lines if isinstance(row, dict)]
    return normalized_boxes, normalized_lines


def _extract_box_from_row(row: dict) -> dict:
    box = row.get("box", {})
    if isinstance(box, dict):
        return box
    return {}


def _box_text(box: dict) -> str:
    for key in ("boxtext", "text"):
        value = box.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _box_tokens(box: dict) -> list[str]:
    text = _box_text(box)
    return [token for token in text.split() if token]


def _primary_object_name(box: dict) -> str:
    maxclass = str(box.get("maxclass", "") or "").strip()
    tokens = _box_tokens(box)
    if maxclass and maxclass not in {"newobj", "message", "comment"}:
        return maxclass
    if tokens:
        return tokens[0]
    return maxclass


def _box_position(box: dict) -> tuple[Any, Any]:
    rect = box.get("patching_rect")
    if isinstance(rect, (list, tuple)) and len(rect) >= 2:
        return rect[0], rect[1]
    return None, None


def _qa_finding(
    *,
    finding_id: str,
    severity: str,
    category: str,
    message: str,
    recommendation: str,
    evidence: Any = None,
) -> dict:
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "message": message,
        "recommendation": recommendation,
        "evidence": evidence,
    }


def _qa_check(
    *,
    check_id: str,
    category: str,
    passed: bool,
    severity_if_failed: str = "low",
    message: str = "",
    evidence: Any = None,
) -> dict:
    return {
        "id": check_id,
        "category": category,
        "passed": bool(passed),
        "severity_if_failed": severity_if_failed,
        "message": message,
        "evidence": evidence,
    }


def collect_patch_audit(
    topology: Any,
    *,
    signal_safety: Any = None,
    signal_safety_error: dict | None = None,
) -> dict:
    boxes, lines = _extract_topology_rows(topology)
    findings: list[dict] = []
    checks: list[dict] = []

    summary = {
        "object_count": len(boxes),
        "connection_count": len(lines),
        "signal_object_count": 0,
        "control_object_count": 0,
        "maxclass_counts": {},
        "objects_without_varname": 0,
    }

    for row in boxes:
        box = _extract_box_from_row(row)
        maxclass = str(box.get("maxclass", "unknown") or "unknown")
        summary["maxclass_counts"][maxclass] = summary["maxclass_counts"].get(maxclass, 0) + 1
        if maxclass.endswith("~"):
            summary["signal_object_count"] += 1
        else:
            summary["control_object_count"] += 1
        if not str(box.get("varname", "") or "").strip():
            summary["objects_without_varname"] += 1

    print_objects: list[dict] = []
    io_debug_objects: list[dict] = []
    todo_comments: list[dict] = []
    non_integer_positions: list[dict] = []
    segmented_lines: list[dict] = []
    missing_varnames: list[dict] = []
    non_local_names: list[dict] = []
    live_ui_without_varname: list[dict] = []
    auto_indexed_names: list[dict] = []
    shared_name_owners = {"s", "send", "r", "receive", "coll", "buffer~", "v", "value"}

    for index, row in enumerate(boxes, start=1):
        box = _extract_box_from_row(row)
        maxclass = str(box.get("maxclass", "") or "")
        varname = str(box.get("varname", "") or "")
        object_name = _primary_object_name(box)
        tokens = _box_tokens(box)
        text = _box_text(box)

        if object_name in {"print", "print~"} or maxclass in {"print", "print~"}:
            print_objects.append({"index": index, "varname": varname, "object": object_name})

        if object_name in {"dac~", "adc~", "ezdac~", "ezadc~"}:
            io_debug_objects.append({"index": index, "varname": varname, "object": object_name})

        if maxclass == "comment" and "todo" in text.lower():
            todo_comments.append({"index": index, "varname": varname, "text": text})

        x, y = _box_position(box)
        if x is not None and y is not None and (not _is_int_like(x) or not _is_int_like(y)):
            non_integer_positions.append({"index": index, "varname": varname, "x": x, "y": y})

        if maxclass not in {"comment"} and not varname:
            missing_varnames.append({"index": index, "object": object_name})

        if object_name in shared_name_owners:
            name_token = ""
            if maxclass == "newobj":
                if len(tokens) >= 2:
                    name_token = tokens[1]
            elif tokens:
                if tokens[0] == object_name and len(tokens) >= 2:
                    name_token = tokens[1]
                else:
                    name_token = tokens[0]
            if (
                name_token
                and not name_token.startswith("---")
                and not name_token.startswith("#0")
                and not name_token.startswith("@")
            ):
                non_local_names.append(
                    {
                        "index": index,
                        "varname": varname,
                        "object": object_name,
                        "name": name_token,
                    }
                )

        if maxclass.startswith("live.") and not varname:
            live_ui_without_varname.append({"index": index, "object": maxclass, "text": text})

        if re.search(r"\[\d+\]$", varname):
            auto_indexed_names.append({"index": index, "varname": varname})

    for index, row in enumerate(lines, start=1):
        patchline = row.get("patchline", {})
        if not isinstance(patchline, dict):
            continue
        midpoints = patchline.get("midpoints")
        if isinstance(midpoints, list) and len(midpoints) >= 2:
            segmented_lines.append(
                {
                    "index": index,
                    "source": patchline.get("source"),
                    "destination": patchline.get("destination"),
                    "midpoints": midpoints,
                }
            )

    checks.extend(
        [
            _qa_check(
                check_id="no_print_objects",
                category="robustness",
                passed=len(print_objects) == 0,
                severity_if_failed="high",
                message="Patch should not contain print objects in release builds.",
                evidence=print_objects[:25],
            ),
            _qa_check(
                check_id="no_debug_io_objects",
                category="robustness",
                passed=len(io_debug_objects) == 0,
                severity_if_failed="high",
                message="Devices should not include direct dac~/adc~ debug I/O in production patches.",
                evidence=io_debug_objects[:25],
            ),
            _qa_check(
                check_id="no_todo_comments",
                category="patch_formatting",
                passed=len(todo_comments) == 0,
                severity_if_failed="medium",
                message="Release patches should not contain TODO comments.",
                evidence=todo_comments[:25],
            ),
            _qa_check(
                check_id="integer_object_positions",
                category="patch_formatting",
                passed=len(non_integer_positions) == 0,
                severity_if_failed="low",
                message="Object positions should use integer coordinates.",
                evidence=non_integer_positions[:25],
            ),
            _qa_check(
                check_id="minimal_segmented_patchcords",
                category="patch_formatting",
                passed=len(segmented_lines) == 0,
                severity_if_failed="low",
                message="Segmented patch cords should be used sparingly.",
                evidence=segmented_lines[:25],
            ),
            _qa_check(
                check_id="local_scope_names",
                category="robustness",
                passed=len(non_local_names) == 0,
                severity_if_failed="medium",
                message="send/receive/coll/buffer names should be device-local with --- or #0 prefixes.",
                evidence=non_local_names[:25],
            ),
            _qa_check(
                check_id="live_ui_has_stable_varname",
                category="parameters",
                passed=len(live_ui_without_varname) == 0,
                severity_if_failed="medium",
                message="live.* UI objects should have stable scripting names where possible.",
                evidence=live_ui_without_varname[:25],
            ),
            _qa_check(
                check_id="no_auto_indexed_parameter_names",
                category="parameters",
                passed=len(auto_indexed_names) == 0,
                severity_if_failed="medium",
                message="Auto-indexed names like [1] should be resolved before release.",
                evidence=auto_indexed_names[:25],
            ),
            _qa_check(
                check_id="object_count_budget",
                category="robustness",
                passed=summary["object_count"] <= 80,
                severity_if_failed="low",
                message="Large root patchers should be encapsulated for readability and maintenance.",
                evidence={"object_count": summary["object_count"], "budget": 80},
            ),
            _qa_check(
                check_id="objects_have_varnames",
                category="patch_formatting",
                passed=len(missing_varnames) == 0,
                severity_if_failed="low",
                message="Objects should have varnames for deterministic automation and refactoring.",
                evidence=missing_varnames[:25],
            ),
        ]
    )

    if isinstance(signal_safety, dict):
        raw_warnings = signal_safety.get("warnings", [])
        warnings = raw_warnings if isinstance(raw_warnings, list) else []
        safe = bool(signal_safety.get("safe", len(warnings) == 0))
        checks.append(
            _qa_check(
                check_id="signal_safety_safe",
                category="signal_safety",
                passed=safe and len(warnings) == 0,
                severity_if_failed="critical",
                message="Signal safety checks should pass without dangerous feedback/gain patterns.",
                evidence=warnings[:25],
            )
        )
    else:
        warnings = []
        safe = None
        checks.append(
            _qa_check(
                check_id="signal_safety_available",
                category="signal_safety",
                passed=False,
                severity_if_failed="low",
                message="Signal safety scan could not be completed.",
                evidence=signal_safety_error or {},
            )
        )

    recommendations = {
        "no_print_objects": "Remove print/print~ objects or gate them behind a development-only path.",
        "no_debug_io_objects": "Replace adc~/dac~ debugging paths with plugin~/plugout~ flow for release.",
        "no_todo_comments": "Resolve TODO comments or remove stale notes before release.",
        "integer_object_positions": "Re-align objects to integer coordinates for clean diffs and patch readability.",
        "minimal_segmented_patchcords": "Reduce segmented cords unless needed for long-distance/backward connections.",
        "local_scope_names": "Prefix shared names with --- (or #0) to prevent cross-device collisions.",
        "live_ui_has_stable_varname": "Assign stable scripting names to live.* UI parameters.",
        "no_auto_indexed_parameter_names": "Rename auto-indexed parameters in View > Parameters to stable names.",
        "object_count_budget": "Encapsulate or split root-level functionality into subpatchers/abstractions.",
        "objects_have_varnames": "Add deterministic varnames to objects used for scripting or maintenance.",
        "signal_safety_safe": "Fix feedback/gain issues and add limiter stages before DAC paths.",
    }
    for check in checks:
        if check["passed"]:
            continue
        check_id = str(check["id"])
        findings.append(
            _qa_finding(
                finding_id=check_id,
                severity=str(check["severity_if_failed"]),
                category=str(check["category"]),
                message=str(check["message"]),
                recommendation=recommendations.get(
                    check_id,
                    "Review this check and address the reported evidence.",
                ),
                evidence=check.get("evidence"),
            )
        )

    if isinstance(signal_safety, dict) and isinstance(warnings, list):
        for index, warning in enumerate(warnings, start=1):
            if not isinstance(warning, dict):
                continue
            warning_type = str(warning.get("type", "SIGNAL_WARNING")).upper()
            severity = "high"
            recommendation = "Inspect and resolve signal-path warning."
            if warning_type in {"FEEDBACK_LOOP", "UNSAFE_FEEDBACK"}:
                severity = "critical"
                recommendation = "Break unsafe feedback loop or stabilize it with explicit delay/attenuation."
            elif warning_type in {"HIGH_GAIN", "NO_LIMITER"}:
                severity = "medium"
                recommendation = "Lower gain and/or add limiter/saturation before output."
            findings.append(
                _qa_finding(
                    finding_id=f"signal_warning_{index}",
                    severity=severity,
                    category="signal_safety",
                    message=str(warning.get("message", "Signal safety warning reported.")),
                    recommendation=recommendation,
                    evidence=warning,
                )
            )

    category_totals: dict[str, dict] = {}
    for check in checks:
        category = str(check["category"])
        row = category_totals.setdefault(
            category,
            {
                "checks": 0,
                "checks_passed": 0,
                "checks_failed": 0,
                "findings": 0,
            },
        )
        row["checks"] += 1
        if check["passed"]:
            row["checks_passed"] += 1
        else:
            row["checks_failed"] += 1

    deductions: list[dict] = []
    for finding in findings:
        severity = str(finding.get("severity", "low")).lower()
        deduction = QA_SEVERITY_WEIGHTS.get(severity, QA_SEVERITY_WEIGHTS["low"])
        deductions.append(
            {
                "finding_id": finding.get("id"),
                "severity": severity,
                "points": deduction,
            }
        )
        category = str(finding.get("category", "uncategorized"))
        row = category_totals.setdefault(
            category,
            {
                "checks": 0,
                "checks_passed": 0,
                "checks_failed": 0,
                "findings": 0,
            },
        )
        row["findings"] += 1

    total_deductions = sum(item["points"] for item in deductions)
    overall_score = max(0.0, 100.0 - float(total_deductions))
    critical_count = sum(1 for finding in findings if finding.get("severity") == "critical")
    high_count = sum(1 for finding in findings if finding.get("severity") == "high")
    medium_count = sum(1 for finding in findings if finding.get("severity") == "medium")
    low_count = sum(1 for finding in findings if finding.get("severity") == "low")
    strict_passed = critical_count == 0 and overall_score >= 80.0

    signal_safety_payload = {
        "available": isinstance(signal_safety, dict),
        "safe": safe,
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
        "warnings": warnings if isinstance(warnings, list) else [],
    }
    if signal_safety_error:
        signal_safety_payload["error"] = signal_safety_error

    return {
        "score": {
            "overall": round(overall_score, 2),
            "max": 100.0,
            "deductions": deductions,
            "total_deductions": total_deductions,
        },
        "summary": {
            **summary,
            "checks_total": len(checks),
            "checks_failed": sum(1 for check in checks if not check["passed"]),
            "findings_total": len(findings),
            "critical_findings": critical_count,
            "high_findings": high_count,
            "medium_findings": medium_count,
            "low_findings": low_count,
        },
        "categories": category_totals,
        "checks": checks,
        "findings": findings,
        "signal_safety": signal_safety_payload,
        "strict_passed": strict_passed,
    }
