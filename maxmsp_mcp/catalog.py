from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path
import hashlib

from .json_utils import read_json_file


def load_flattened_docs(docs_path: Path) -> dict[str, dict]:
    docs = read_json_file(docs_path)

    flattened_docs: dict[str, dict] = {}
    for obj_list in docs.values():
        for obj in obj_list:
            flattened_docs[obj["name"]] = obj
    return flattened_docs


class MaxPyCatalog:
    """Read-only index over vendored MaxPyLang object metadata."""

    def __init__(self, root: Path):
        self.root = root
        self.obj_info_dir = self.root / "data" / "OBJ_INFO"
        self.aliases_file = self.obj_info_dir / "obj_aliases.json"
        self._index: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        self._reverse_aliases: dict[str, list[str]] = {}
        self._schema_cache: dict[str, dict] = {}
        self.available = False
        self.schema_hash = ""
        self._init()

    def _init(self) -> None:
        if not self.obj_info_dir.exists():
            return

        digest = hashlib.sha256()
        files = sorted(self.obj_info_dir.glob("*/*.json"))
        for file_path in files:
            name = file_path.stem
            package = file_path.parent.name
            self._index[name] = {
                "name": name,
                "package": package,
                "path": file_path,
            }
            digest.update(file_path.relative_to(self.obj_info_dir).as_posix().encode("utf-8"))
            st = file_path.stat()
            digest.update(f":{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))

        if self.aliases_file.exists():
            try:
                alias_payload = read_json_file(self.aliases_file)
                if isinstance(alias_payload, dict):
                    self._aliases = {str(k): str(v) for k, v in alias_payload.items()}
                digest.update(self.aliases_file.read_bytes())
            except Exception:
                self._aliases = {}

        for alias, canonical in self._aliases.items():
            self._reverse_aliases.setdefault(canonical, []).append(alias)

        self.schema_hash = digest.hexdigest()[:16]
        self.available = len(self._index) > 0

    @property
    def count(self) -> int:
        return len(self._index)

    @property
    def packages(self) -> list[str]:
        return sorted({entry["package"] for entry in self._index.values()})

    def resolve_name(self, object_name: str) -> tuple[str, bool]:
        canonical = self._aliases.get(object_name, object_name)
        return canonical, canonical != object_name

    def aliases_for(self, object_name: str) -> list[str]:
        return sorted(self._reverse_aliases.get(object_name, []))

    def _load_schema(self, object_name: str) -> dict | None:
        if object_name in self._schema_cache:
            return self._schema_cache[object_name]

        entry = self._index.get(object_name)
        if not entry:
            return None
        try:
            schema = read_json_file(entry["path"])
            if isinstance(schema, dict):
                self._schema_cache[object_name] = schema
                return schema
        except Exception:
            return None
        return None

    def get_schema(self, object_name: str) -> dict | None:
        canonical, via_alias = self.resolve_name(object_name)
        entry = self._index.get(canonical)
        if not entry:
            return None
        schema = self._load_schema(canonical)
        if not isinstance(schema, dict):
            return None
        default_box = schema.get("default", {}).get("box", {})
        return {
            "query": object_name,
            "canonical_name": canonical,
            "resolved_via_alias": via_alias,
            "package": entry["package"],
            "path": str(entry["path"]),
            "aliases": self.aliases_for(canonical),
            "summary": {
                "maxclass": default_box.get("maxclass"),
                "numinlets": default_box.get("numinlets"),
                "numoutlets": default_box.get("numoutlets"),
                "default_text": default_box.get("text"),
            },
            "schema": schema,
        }

    def suggest(self, object_name: str, limit: int = 5) -> list[str]:
        universe = sorted(set(self._index.keys()) | set(self._aliases.keys()))
        return get_close_matches(object_name, universe, n=limit, cutoff=0.6)

    def search(
        self,
        query: str,
        *,
        package: str | None = None,
        limit: int = 20,
        include_aliases: bool = True,
    ) -> list[dict]:
        query_lc = query.lower().strip()
        if not query_lc:
            return []

        rows: list[tuple[int, str, dict]] = []
        for name, entry in self._index.items():
            if package and entry["package"] != package:
                continue
            score = None
            name_lc = name.lower()
            if name_lc == query_lc:
                score = 0
            elif name_lc.startswith(query_lc):
                score = 1
            elif query_lc in name_lc:
                score = 2

            alias_matches: list[str] = []
            if include_aliases:
                for alias in self._reverse_aliases.get(name, []):
                    alias_lc = alias.lower()
                    if alias_lc == query_lc:
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 0)
                    elif alias_lc.startswith(query_lc):
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 1)
                    elif query_lc in alias_lc:
                        alias_matches.append(alias)
                        score = min(score if score is not None else 3, 2)

            if score is None:
                continue

            rows.append(
                (
                    score,
                    name,
                    {
                        "name": name,
                        "package": entry["package"],
                        "aliases": sorted(alias_matches),
                    },
                )
            )

        rows.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in rows[: max(1, min(limit, 100))]]

    def io_counts(self, object_name: str) -> tuple[int | None, int | None]:
        schema_info = self.get_schema(object_name)
        if not schema_info:
            return None, None
        summary = schema_info.get("summary", {})
        numinlets = summary.get("numinlets")
        numoutlets = summary.get("numoutlets")
        if not isinstance(numinlets, int):
            numinlets = None
        if not isinstance(numoutlets, int):
            numoutlets = None
        return numinlets, numoutlets
