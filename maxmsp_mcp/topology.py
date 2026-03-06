from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any

from .json_utils import canonical_json, read_json_file, write_json_file


ERROR_VALIDATION = "VALIDATION_ERROR"
ERROR_PRECONDITION = "PRECONDITION_FAILED"


class TopologyError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        recoverable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.recoverable = recoverable
        self.details = details or {}


def clone_json_value(value: Any) -> Any:
    return deepcopy(value)


# Backward-compatible alias used by server.py and older tests.
clone_json_data = clone_json_value


def _stable_value(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        rendered = value if math.isfinite(value) else repr(value)
        return ("float", rendered)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, dict):
        items = tuple(
            sorted(
                ((str(key), _stable_value(item_value)) for key, item_value in value.items()),
                key=lambda item: item[0],
            )
        )
        return ("dict",) + items
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_stable_value(item) for item in value))
    return ("repr", repr(value))


@dataclass(frozen=True)
class CanonicalBox:
    varname: Any
    maxclass: Any
    patching_rect: Any
    numinlets: Any
    numoutlets: Any
    boxtext: Any
    attributes: Any

    def sort_key(self) -> tuple[Any, ...]:
        return (
            str(self.varname or ""),
            str(self.maxclass or ""),
            _stable_value(self.patching_rect),
            str(self.boxtext or ""),
            _stable_value(self.attributes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "varname": self.varname,
            "maxclass": self.maxclass,
            "patching_rect": self.patching_rect,
            "numinlets": self.numinlets,
            "numoutlets": self.numoutlets,
            "boxtext": self.boxtext,
            "attributes": self.attributes,
        }


@dataclass(frozen=True)
class CanonicalLine:
    source: Any
    destination: Any

    def sort_key(self) -> tuple[Any, ...]:
        return (
            _stable_value(self.source),
            _stable_value(self.destination),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
        }


class TopologySnapshot:
    def __init__(self, boxes: list[dict[str, Any]] | None = None, lines: list[dict[str, Any]] | None = None):
        self.boxes = clone_json_value(boxes or [])
        self.lines = clone_json_value(lines or [])
        self._canonical_cache: dict[str, list[dict[str, Any]]] | None = None
        self._signature_cache: tuple[tuple[Any, ...], tuple[Any, ...]] | None = None
        self._counts_cache: tuple[int, int] | None = None
        self._digest_cache: str | None = None
        self._varnames_cache: set[str] | None = None

    @classmethod
    def from_payload(cls, topology: Any) -> "TopologySnapshot":
        if not isinstance(topology, dict):
            return cls()
        return cls(
            boxes=topology.get("boxes", []) if isinstance(topology.get("boxes"), list) else [],
            lines=topology.get("lines", []) if isinstance(topology.get("lines"), list) else [],
        )

    def to_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "boxes": clone_json_value(self.boxes),
            "lines": clone_json_value(self.lines),
        }

    def canonical_payload(self) -> dict[str, list[dict[str, Any]]]:
        if self._canonical_cache is None:
            boxes: list[CanonicalBox] = []
            lines: list[CanonicalLine] = []
            for item in self.boxes:
                if not isinstance(item, dict):
                    continue
                box = item.get("box", {})
                if not isinstance(box, dict):
                    continue
                boxes.append(
                    CanonicalBox(
                        varname=box.get("varname"),
                        maxclass=box.get("maxclass"),
                        patching_rect=box.get("patching_rect"),
                        numinlets=box.get("numinlets"),
                        numoutlets=box.get("numoutlets"),
                        boxtext=box.get("boxtext"),
                        attributes=box.get("attributes"),
                    )
                )
            for item in self.lines:
                if not isinstance(item, dict):
                    continue
                line = item.get("patchline", {})
                if not isinstance(line, dict):
                    continue
                lines.append(
                    CanonicalLine(
                        source=line.get("source"),
                        destination=line.get("destination"),
                    )
                )
            canonical_boxes = [row.to_dict() for row in sorted(boxes, key=lambda row: row.sort_key())]
            canonical_lines = [row.to_dict() for row in sorted(lines, key=lambda row: row.sort_key())]
            self._canonical_cache = {
                "boxes": canonical_boxes,
                "lines": canonical_lines,
            }
            self._signature_cache = (
                tuple(_stable_value(row) for row in canonical_boxes),
                tuple(_stable_value(row) for row in canonical_lines),
            )
            self._counts_cache = (len(canonical_boxes), len(canonical_lines))
        return clone_json_value(self._canonical_cache)

    def digest_counts(self) -> tuple[str, int, int]:
        self.canonical_payload()
        assert self._signature_cache is not None
        assert self._counts_cache is not None
        if self._digest_cache is None:
            self._digest_cache = hashlib.sha256(
                canonical_json(self._signature_cache).encode("utf-8")
            ).hexdigest()
        object_count, connection_count = self._counts_cache
        return self._digest_cache, object_count, connection_count

    def is_empty(self) -> bool:
        return len(self.boxes) == 0 and len(self.lines) == 0

    def varnames(self) -> set[str]:
        if self._varnames_cache is None:
            names: set[str] = set()
            for row in self.boxes:
                if not isinstance(row, dict):
                    continue
                box = row.get("box", row)
                if not isinstance(box, dict):
                    continue
                varname = box.get("varname")
                if isinstance(varname, str) and varname:
                    names.add(varname)
            self._varnames_cache = names
        return set(self._varnames_cache)


def extract_topology_with_format(payload: Any) -> tuple[str, dict[str, list[dict[str, Any]]]] | None:
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("boxes"), list) and isinstance(payload.get("lines"), list):
        return "topology", TopologySnapshot.from_payload(payload).to_payload()

    patcher = payload.get("patcher")
    if isinstance(patcher, dict) and isinstance(patcher.get("boxes"), list) and isinstance(
        patcher.get("lines"), list
    ):
        return "maxpat_patcher", TopologySnapshot.from_payload(patcher).to_payload()
    return None


def extract_topology_from_payload(payload: Any) -> dict[str, list[dict[str, Any]]]:
    extracted = extract_topology_with_format(payload)
    if extracted is None:
        return {"boxes": [], "lines": []}
    return extracted[1]


def merge_topologies(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    merged_boxes = clone_json_value(base.get("boxes", []))
    merged_boxes.extend(clone_json_value(incoming.get("boxes", [])))

    line_seen: set[tuple[str, int, str, int]] = set()
    merged_lines: list[dict[str, Any]] = []
    for source_topology in (base, incoming):
        for row in source_topology.get("lines", []):
            if not isinstance(row, dict):
                continue
            patchline = row.get("patchline", row)
            if not isinstance(patchline, dict):
                continue
            source = patchline.get("source", [])
            destination = patchline.get("destination", [])
            if (
                not isinstance(source, list)
                or not isinstance(destination, list)
                or len(source) < 2
                or len(destination) < 2
            ):
                continue
            if not isinstance(source[0], str) or not isinstance(destination[0], str):
                continue
            try:
                src_idx = int(source[1])
                dst_idx = int(destination[1])
            except Exception:
                continue
            key = (source[0], src_idx, destination[0], dst_idx)
            if key in line_seen:
                continue
            line_seen.add(key)
            merged_lines.append(
                {
                    "patchline": {
                        "source": [source[0], src_idx],
                        "destination": [destination[0], dst_idx],
                    }
                }
            )
    return {"boxes": merged_boxes, "lines": merged_lines}


def generate_unique_varname(base: str, used: set[str]) -> str:
    safe_base = base if base else "imp_obj"
    candidate = safe_base
    suffix = 1
    while candidate in used:
        candidate = f"{safe_base}__imp{suffix}"
        suffix += 1
    return candidate


def normalize_import_topology(
    topology: dict[str, Any],
    *,
    reserved_varnames: set[str] | None = None,
    auto_rename_collisions: bool = True,
) -> dict[str, Any]:
    snapshot = TopologySnapshot.from_payload(topology).to_payload()
    reserved = set(reserved_varnames or set())
    used = set(reserved)
    seen_source: set[str] = set()
    remap: dict[str, str] = {}
    source_ref_map: dict[str, str] = {}
    collisions = 0
    generated_varnames = 0
    id_ref_remaps = 0
    normalized_boxes: list[dict[str, Any]] = []

    for idx, row in enumerate(snapshot["boxes"]):
        if not isinstance(row, dict):
            continue
        box = row.get("box", row)
        if not isinstance(box, dict):
            continue
        normalized_box = clone_json_value(box)
        original_varname = normalized_box.get("varname")
        source_varname = original_varname.strip() if isinstance(original_varname, str) else ""

        if source_varname:
            if source_varname in seen_source:
                raise TopologyError(
                    ERROR_VALIDATION,
                    f"Source topology has duplicate varname '{source_varname}'.",
                    recoverable=False,
                )
            seen_source.add(source_varname)
            if source_varname in used:
                if not auto_rename_collisions:
                    raise TopologyError(
                        ERROR_PRECONDITION,
                        f"Varname collision detected for '{source_varname}'.",
                        hint="Retry with auto_rename_collisions=True.",
                        recoverable=False,
                    )
                renamed = generate_unique_varname(source_varname, used)
                normalized_box["varname"] = renamed
                remap[source_varname] = renamed
                collisions += 1
                used.add(renamed)
            else:
                normalized_box["varname"] = source_varname
                used.add(source_varname)
        else:
            generated = generate_unique_varname(f"imp_obj_{idx + 1}", used)
            normalized_box["varname"] = generated
            generated_varnames += 1
            used.add(generated)

        final_varname = normalized_box.get("varname")
        if isinstance(final_varname, str) and final_varname:
            if source_varname:
                source_ref_map[source_varname] = final_varname
            source_id = normalized_box.get("id")
            if isinstance(source_id, str) and source_id:
                if source_ref_map.get(source_id) != final_varname:
                    source_ref_map[source_id] = final_varname
                    if source_id != final_varname:
                        id_ref_remaps += 1

        normalized_boxes.append({"box": normalized_box})

    valid_varnames = {
        row["box"]["varname"]
        for row in normalized_boxes
        if isinstance(row, dict)
        and isinstance(row.get("box"), dict)
        and isinstance(row["box"].get("varname"), str)
        and row["box"]["varname"]
    }

    normalized_lines: list[dict[str, Any]] = []
    skipped_lines = 0
    for row in snapshot["lines"]:
        if not isinstance(row, dict):
            skipped_lines += 1
            continue
        patchline = row.get("patchline", row)
        if not isinstance(patchline, dict):
            skipped_lines += 1
            continue
        source = list(patchline.get("source") or [])
        destination = list(patchline.get("destination") or [])
        if len(source) < 2 or len(destination) < 2:
            skipped_lines += 1
            continue

        src_var, src_idx = source[0], source[1]
        dst_var, dst_idx = destination[0], destination[1]

        if isinstance(src_var, str):
            src_var = source_ref_map.get(src_var, remap.get(src_var, src_var))
        if isinstance(dst_var, str):
            dst_var = source_ref_map.get(dst_var, remap.get(dst_var, dst_var))

        if not isinstance(src_var, str) or not isinstance(dst_var, str):
            skipped_lines += 1
            continue

        if not isinstance(src_idx, int):
            try:
                src_idx = int(src_idx)
            except Exception:
                skipped_lines += 1
                continue
        if not isinstance(dst_idx, int):
            try:
                dst_idx = int(dst_idx)
            except Exception:
                skipped_lines += 1
                continue

        if src_var not in valid_varnames or dst_var not in valid_varnames:
            skipped_lines += 1
            continue

        normalized_lines.append(
            {"patchline": {"source": [src_var, src_idx], "destination": [dst_var, dst_idx]}}
        )

    return {
        "topology": {"boxes": normalized_boxes, "lines": normalized_lines},
        "varname_remap": remap,
        "collisions_count": collisions,
        "generated_varnames": generated_varnames,
        "skipped_lines": skipped_lines,
        "line_ref_map_size": len(source_ref_map),
        "line_ref_id_mappings": id_ref_remaps,
    }


def patch_payload_from_template(
    template: dict[str, Any],
    topology: dict[str, Any],
) -> dict[str, Any]:
    payload = clone_json_value(template) if isinstance(template, dict) else {}
    patcher = payload.get("patcher")
    if not isinstance(patcher, dict):
        patcher = {}
        payload["patcher"] = patcher
    normalized = TopologySnapshot.from_payload(topology).to_payload()
    patcher["boxes"] = normalized["boxes"]
    patcher["lines"] = normalized["lines"]
    return payload


class Topology(TopologySnapshot):
    @classmethod
    def from_payload(cls, topology: Any) -> "Topology":
        snapshot = TopologySnapshot.from_payload(topology)
        return cls(snapshot.boxes, snapshot.lines)

    def to_patch_payload(self, template: dict[str, Any]) -> dict[str, Any]:
        return patch_payload_from_template(template, self.to_payload())


def topology_hash(topology: dict[str, Any]) -> tuple[str, int, int]:
    return Topology.from_payload(topology).digest_counts()


def topology_varnames(topology: dict[str, Any]) -> set[str]:
    return Topology.from_payload(topology).varnames()


def is_topology_empty(topology: dict[str, Any]) -> bool:
    return Topology.from_payload(topology).is_empty()


def write_patch_payload(path: Path, topology: dict[str, Any], template: dict[str, Any]) -> None:
    write_json_file(path, Topology.from_payload(topology).to_patch_payload(template))


def load_patch_topology(path: Path) -> dict[str, Any]:
    try:
        payload = read_json_file(path)
    except Exception as exc:
        raise TopologyError(
            ERROR_VALIDATION,
            f"Failed to parse JSON at {path}: {exc}",
            recoverable=True,
        ) from exc

    extracted = extract_topology_with_format(payload)
    if extracted is None:
        raise TopologyError(
            ERROR_VALIDATION,
            (
                f"File {path} is not a supported patch payload. "
                "Expected either top-level boxes/lines or patcher.boxes/patcher.lines."
            ),
            recoverable=True,
        )

    detected_format, topology = extracted
    digest, object_count, connection_count = topology_hash(topology)
    return {
        "format": detected_format,
        "topology": topology,
        "hash": digest,
        "object_count": object_count,
        "connection_count": connection_count,
    }
