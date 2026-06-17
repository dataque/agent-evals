"""Tool-result contract registry (for metric #4, reused by #24).

The frontend declares, per tool, the shape of that tool's result (TS types / zod
/ GraphQL fragments). We mirror those as versioned JSON Schemas keyed by tool
name; metric #4 validates each emitted ``TOOL_CALL_RESULT`` payload against the
registered schema. Schemas are intentionally strict on the top-level shape and
key field types, lenient on deep nesting (``additionalProperties`` allowed), so
the check tracks the contract without over-fitting to incidental fields.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import jsonschema


class ContractRegistry:
    def __init__(self, schemas: dict[str, dict], version: str = "v1") -> None:
        self.schemas = schemas
        self.version = version

    def schema_for(self, tool_name: str | None) -> dict | None:
        if tool_name is None:
            return None
        return self.schemas.get(tool_name)

    def has(self, tool_name: str | None) -> bool:
        return self.schema_for(tool_name) is not None

    def tools(self) -> list[str]:
        return sorted(self.schemas)

    def validate(self, tool_name: str | None, payload: object) -> tuple[bool, str | None]:
        """Return ``(is_valid, error_message)``. A tool with no registered
        schema is reported valid (``True, None``) — "not applicable", not a fail.
        """
        schema = self.schema_for(tool_name)
        if schema is None:
            return True, None
        try:
            jsonschema.validate(payload, schema)
            return True, None
        except jsonschema.ValidationError as exc:
            loc = "/".join(str(p) for p in exc.absolute_path)
            return False, f"{loc}: {exc.message}" if loc else exc.message


@functools.lru_cache(maxsize=None)
def get_registry(version: str = "v1") -> ContractRegistry:
    base = Path(__file__).parent / "tools" / version
    schemas: dict[str, dict] = {}
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            schemas[path.stem] = json.loads(path.read_text())
    return ContractRegistry(schemas, version=version)
