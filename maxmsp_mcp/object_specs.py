from __future__ import annotations

import math
from typing import Any

from .protocol import ERROR_VALIDATION


FLOAT_REQUIRED_OBJECTS = {"+", "-", "*", "/", "!+", "!-", "!*", "!/", "%", "pow", "scale"}
PACK_OBJECTS = {"pack", "pak", "unpack"}
REJECTED_OBJECTS = {
    "times~": "*~",
}
MIN_ARGS_OBJECTS = {
    "comb~": {
        "min_args": 5,
        "usage": "[comb~ maxdelay delay feedback feedforward gain] e.g. [comb~ 1000 100 0.9 0.5 1.]",
    },
}
PARAM_RANGE_CHECKS = {
    "svf~": {
        "arg_index": 1,
        "check": lambda v: v >= 1,
        "error": "svf~ Q/resonance should be 0-1, not 0-100. Got {value}. "
        "Set extend=True if you really want Q >= 1.",
    },
    "onepole~": {
        "arg_index": 0,
        "check": lambda v: v < 10,
        "error": "onepole~ takes frequency in Hz (e.g., 5000), not a coefficient. Got {value}. "
        "Set extend=True if you really want frequency < 10 Hz.",
    },
}


def _error_result(
    message: str,
    *,
    hint: str | None = None,
    recoverable: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error = {
        "code": ERROR_VALIDATION,
        "message": message,
        "recoverable": recoverable,
        "details": details or {},
    }
    if hint:
        error["hint"] = hint
    return {"success": False, "error": error}


def has_float_arg(args: list[Any]) -> bool:
    for arg in args:
        if isinstance(arg, float):
            return True
        if isinstance(arg, str) and "." in arg:
            try:
                float(arg)
                return True
            except ValueError:
                pass
    return False


def pack_has_float_arg(args: list[Any]) -> bool:
    for arg in args:
        if isinstance(arg, float):
            return True
        if isinstance(arg, str) and arg.lower() == "f":
            return True
        if isinstance(arg, str) and "." in arg:
            try:
                float(arg)
                return True
            except ValueError:
                pass
    return False


def convert_string_args(args: list[Any]) -> list[Any]:
    converted: list[Any] = []
    for arg in args:
        if isinstance(arg, str):
            if "." in arg:
                try:
                    converted.append(float(arg))
                    continue
                except ValueError:
                    pass
            else:
                try:
                    converted.append(int(arg))
                    continue
                except ValueError:
                    pass
        converted.append(arg)
    return converted


def normalize_add_object_spec(obj_type: Any, args: Any) -> tuple[str, list[Any], dict | None, dict | None]:
    normalized_obj_type = obj_type.strip() if isinstance(obj_type, str) else str(obj_type or "").strip()
    normalized_args = list(args) if isinstance(args, list) else []

    if normalized_obj_type.lower() == "newobj":
        return (
            normalized_obj_type,
            normalized_args,
            None,
            _error_result(
                "obj_type='newobj' is no longer accepted.",
                hint="Use the actual Max object name directly, for example obj_type='prepend'.",
                recoverable=True,
            ),
        )
    return normalized_obj_type, normalized_args, None, None


def normalize_avoid_rect_payload(payload: Any) -> tuple[list[float], bool]:
    candidate = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("avoid_rect"), (list, tuple)):
            candidate = payload.get("avoid_rect")
        elif all(key in payload for key in ("left", "top", "right", "bottom")):
            candidate = [
                payload.get("left"),
                payload.get("top"),
                payload.get("right"),
                payload.get("bottom"),
            ]
        elif isinstance(payload.get("results"), (list, tuple)):
            candidate = payload.get("results")

    if not isinstance(candidate, (list, tuple)) or len(candidate) != 4:
        return [0.0, 0.0, 0.0, 0.0], False

    normalized: list[float] = []
    for value in candidate:
        try:
            parsed = float(value)
        except Exception:
            return [0.0, 0.0, 0.0, 0.0], False
        if not math.isfinite(parsed):
            return [0.0, 0.0, 0.0, 0.0], False
        normalized.append(parsed)
    return normalized, True


def validate_add_object_payload(
    *,
    obj_type: str,
    args: list[Any],
    int_mode: bool,
    extend: bool,
    use_live_dial: bool,
    trigger_rtl: bool,
) -> dict[str, Any] | None:
    if not isinstance(obj_type, str) or not obj_type.strip():
        return _error_result("Object type must be a non-empty string.")
    if not isinstance(args, list):
        return _error_result("Object args must be a list.")

    if obj_type in REJECTED_OBJECTS:
        correct = REJECTED_OBJECTS[obj_type]
        return _error_result(f"WRONG OBJECT: '{obj_type}' does not exist. Use '{correct}' instead.")

    if obj_type in MIN_ARGS_OBJECTS:
        req = MIN_ARGS_OBJECTS[obj_type]
        if len(args) < req["min_args"]:
            return _error_result(
                f"MISSING ARGUMENTS: '{obj_type}' requires at least {req['min_args']} arguments. Usage: {req['usage']}"
            )

    if obj_type in FLOAT_REQUIRED_OBJECTS:
        scale_float_intent = False
        if obj_type == "scale" and len(args) >= 4:
            out_min, out_max = args[2], args[3]
            if isinstance(out_min, (int, float)) and isinstance(out_max, (int, float)):
                if abs(out_max - out_min) <= 2:
                    scale_float_intent = True
        if not has_float_arg(args) and not int_mode and not scale_float_intent:
            return _error_result(
                f"FLOAT REQUIRED: '{obj_type}' defaults to integer mode which truncates floats. "
                f"Use STRING args with '.' to preserve float type (JSON strips .0 from numbers). "
                f"Example: args: [\"0\", \"127\", \"0\", \"25.\"] instead of [0, 127, 0, 25.0]. "
                f"Or set int_mode=True if integer truncation is intended."
            )

    if obj_type in PACK_OBJECTS:
        if not pack_has_float_arg(args) and not int_mode:
            return _error_result(
                f"FLOAT REQUIRED: '{obj_type}' with integer arguments outputs integers. "
                f"Use 'f' type specifier: ['f', 'f', 'f'], or STRING args with '.': [\"0.\", \"0.\"], "
                f"or set int_mode=True if integer output is intended."
            )

    if obj_type in PARAM_RANGE_CHECKS and not extend:
        check = PARAM_RANGE_CHECKS[obj_type]
        idx = check["arg_index"]
        if len(args) > idx:
            value = args[idx]
            if isinstance(value, (int, float)) and check["check"](value):
                return _error_result(f"PARAM RANGE: {check['error'].format(value=value)}")

    if obj_type == "live.dial" and not use_live_dial:
        return _error_result(
            "USE DIAL INSTEAD: live.dial outputs 0-127 with no inline range control. "
            "Use [dial] with attributes instead:\n"
            "  - Float 0-1: [dial @size 1 @floatoutput 1]\n"
            "  - Float -1 to 1 (pan): [dial @min -1 @size 2 @floatoutput 1 @mode 6]\n"
            "  - Int 0-127: [dial @size 127]\n"
            "Set use_live_dial=True only if you specifically need Live integration."
        )

    if obj_type == "dial":
        if "@size" not in args:
            return _error_result(
                "RANGE REQUIRED: dial needs explicit @size attribute. Examples:\n"
                "  - Float 0-1: ['@size', 1, '@floatoutput', 1]\n"
                "  - Float -1 to 1 (pan): ['@min', -1, '@size', 2, '@floatoutput', 1, '@mode', 6]\n"
                "  - Int 0-127: ['@size', 127]"
            )
        if not extend:
            try:
                size_idx = args.index("@size")
                if size_idx + 1 < len(args):
                    size_val = args[size_idx + 1]
                    if isinstance(size_val, (int, float)) and size_val > 255:
                        return _error_result(
                            f"DIAL SIZE TOO LARGE: @size {int(size_val)} creates unusable UI "
                            f"(must drag through {int(size_val)} positions). "
                            "For large ranges, use:\n"
                            "  - [flonum] or [number] for direct value entry\n"
                            "  - A scaled dial (e.g., 0-100 dial with multiplier)\n"
                            "Set extend=True to bypass this check."
                        )
            except (ValueError, IndexError):
                pass

    if obj_type in {"trigger", "t"} and not trigger_rtl:
        return _error_result(
            "ORDER ACKNOWLEDGMENT REQUIRED: trigger/t fires outlets RIGHT-TO-LEFT. "
            "The rightmost argument fires FIRST. For example, [t b f] sends 'f' first, then 'b'. "
            "Set trigger_rtl=True to acknowledge you understand this."
        )

    if obj_type == "coll":
        has_embed = False
        for index, arg in enumerate(args):
            if arg == "@embed" and index + 1 < len(args) and args[index + 1] == 1:
                has_embed = True
                break
        if not has_embed:
            return _error_result(
                "EMBED REQUIRED: coll data does not persist on save unless @embed 1 is set. "
                "Use args like: ['mycoll', '@embed', 1] to ensure data is saved with the patch."
            )

    return None
