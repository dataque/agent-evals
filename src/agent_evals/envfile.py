"""Minimal ``.env`` loader + ``${VAR}`` expansion — no external dependency.

The CLI auto-loads ``.env`` (gitignored) so per-developer values like the caller
GPN, bearer tokens, and judge credentials stay out of the committed config.
Config string values may reference env vars as ``${VAR}`` or ``${VAR:-default}``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv(path: str | os.PathLike = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Existing env vars win unless ``override`` is set. Supports ``#`` comments,
    a leading ``export``, and single/double-quoted values. Missing file = no-op.
    """
    p = Path(path)
    if not p.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if override or key not in os.environ:
            os.environ[key] = val
        loaded[key] = val
    return loaded


def expand_env(obj):
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in strings of a
    JSON-like structure (dicts/lists/strings). Unset vars with no default → ''."""
    if isinstance(obj, str):
        def repl(m: re.Match) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj
