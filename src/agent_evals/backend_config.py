"""Record what the backend's own ``application.yaml`` declares (E19).

A run bundle can now carry three different kinds of claim about the system under
test's LLM, and they are worth keeping apart because they fail in different ways:

- **observed** (``backend``): read from the running process through its actuator
  meter. Trustworthy, but a Micrometer meter carries only low-cardinality tags,
  so it can never report reasoning effort or any other request option.
- **declared** (``backend``): the operator's word, from ``targets.yaml`` or a
  CLI flag. Covers anything, and is only as truthful as whoever typed it.
- **configured in source** (``backend_config``, this module): what the backend's
  configuration file says. It covers the options a meter cannot carry, without
  asking anyone to retype them, and it is checkable: the file has a digest.

The third is NOT a substitute for the first. A config file describes the build it
came from; the process may have been started from a different build, and Spring
lets an environment variable override any property at boot without touching the
file. A disagreement between this section and the observed one is a real finding
(it means the deployment is not running what its source says), which is why this
lands in its OWN top-level section rather than being merged into ``backend``.

Accepts a plain YAML file, a directory containing one, or a Spring Boot fat jar
(read from ``BOOT-INF/classes/``). Give several paths and they are merged left to
right, mirroring how Spring layers a profile over the base file.

Every failure is non-fatal, and secrets are never recorded.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import yaml

from .transport.actuator import OPTION_FIELDS, collect_option_fields, normalise_key

_JAR_MEMBERS = ("BOOT-INF/classes/application.yaml", "BOOT-INF/classes/application.yml")
_DIR_MEMBERS = ("application.yaml", "application.yml")

# Beyond the identity fields the actuator probe already knows about, these are
# the request options worth carrying into a bundle. Keyed by normalised name so
# `maxTokens`, `max-tokens` and `MAX_TOKENS` all land on the same field.
_EXTRA_FIELDS = {
    "temperature": "temperature",
    "maxtokens": "max_tokens",
    "maxcompletiontokens": "max_completion_tokens",
    "topp": "top_p",
    "verbosity": "verbosity",
    "servicetier": "service_tier",
    "timeout": "timeout",
    "maxretries": "max_retries",
}

# A value whose KEY looks like a credential or an endpoint is never recorded,
# whatever it holds. Endpoints are in the list for the same reason the judge block
# records a deployment name but never an endpoint (see
# ``test_describe_judge_never_reports_credentials``): a host name is
# infrastructure, not provenance, and it makes an artifact unsafe to paste.
_SECRET_MARKERS = ("key", "secret", "password", "passwd", "token", "credential", "cert",
                   "baseurl", "endpoint", "uri")
REDACTED = "***redacted***"


def _is_secret(key: object) -> bool:
    norm = normalise_key(key)
    return any(marker in norm for marker in _SECRET_MARKERS)


def redact(node: object) -> object:
    """Copy a config subtree with credential-looking values replaced.

    Applied by key name, not by value, so a literal secret is caught even when it
    looks like ordinary text, and a ``${PLACEHOLDER}`` is caught too (its name
    still tells a reader which credential the deployment uses).
    """
    if isinstance(node, dict):
        return {k: (REDACTED if _is_secret(k) else redact(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [redact(item) for item in node]
    return node


def deep_merge(base: dict, overlay: dict) -> dict:
    """Layer one config document over another, as Spring layers a profile."""
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_source(path: str | Path) -> tuple[str, bytes]:
    """Return ``(label, raw bytes)`` for a YAML file, a directory, or a fat jar."""
    p = Path(path)
    if p.is_dir():
        for name in _DIR_MEMBERS:
            if (p / name).is_file():
                return str(p / name), (p / name).read_bytes()
        raise FileNotFoundError(f"no application.yaml in {p}")
    if p.suffix == ".jar":
        with zipfile.ZipFile(p) as jar:
            names = set(jar.namelist())
            for member in _JAR_MEMBERS:
                if member in names:
                    return f"{p}!/{member}", jar.read(member)
        raise FileNotFoundError(f"no BOOT-INF/classes/application.yaml in {p}")
    return str(p), p.read_bytes()


def _load_documents(raw: bytes) -> dict:
    """Parse a Spring config file, merging its ``---`` documents in order."""
    merged: dict = {}
    for doc in yaml.safe_load_all(raw.decode("utf-8")):
        if isinstance(doc, dict):
            merged = deep_merge(merged, doc)
    return merged


def _spring_ai(config: dict) -> dict:
    return ((config.get("spring") or {}).get("ai") or {}) if isinstance(config, dict) else {}


def extract_llm(config: dict) -> dict:
    """The chat options the file declares, from anywhere under ``spring.ai``.

    Swept rather than read from a fixed path because the property lives in a
    different place per provider (``spring.ai.openai.chat`` vs
    ``spring.ai.azure.openai.chat.options``) and Spring AI accepts both the
    flattened and the nested form for the same setting.
    """
    ai = _spring_ai(config)
    if not ai:
        return {}
    out: dict = {}
    collect_option_fields(ai, out)  # model / deployment / reasoning_effort / api_version
    _collect_extra(ai, out)
    return out


def _collect_extra(node: object, out: dict) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _collect_extra(value, out)
                continue
            field = _EXTRA_FIELDS.get(normalise_key(key))
            if not field or field in out or _is_secret(key):
                continue
            text = str(value).strip() if value is not None else ""
            if text:
                out[field] = text
    elif isinstance(node, list):
        for item in node:
            _collect_extra(item, out)


def extract_actuator(config: dict) -> dict:
    """What the file says the actuator exposes.

    Recorded because it explains this run's own probe result in the same
    artifact: a ``probe`` block full of 404s next to ``include: health,metrics``
    is a settled question rather than something to re-investigate later.
    """
    mgmt = (config or {}).get("management") or {}
    exposure = (((mgmt.get("endpoints") or {}).get("web") or {}).get("exposure") or {})
    out: dict = {}
    include = exposure.get("include")
    if include:
        out["exposure_include"] = include
    enable = (mgmt.get("metrics") or {}).get("enable")
    if isinstance(enable, dict):
        out["metrics_enable"] = enable
    return out


def describe_backend_config(paths) -> dict:
    """The ``backend_config`` section of ``params.json``. Never raises.

    ``paths`` is one path or several. Each is recorded with its own digest, so a
    reader can tell whether two runs were produced by the same configuration
    without trusting the values to have been copied correctly. A path that cannot
    be read becomes an ``error`` entry rather than a missing file nobody notices.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [p for p in (paths or []) if str(p).strip()]
    if not paths:
        return {}

    files: list[dict] = []
    merged: dict = {}
    for path in paths:
        entry: dict = {"path": str(path)}
        try:
            label, raw = _read_source(path)
        except Exception as exc:  # noqa: BLE001 - provenance must not fail a run
            entry["error"] = f"{type(exc).__name__}: {exc}"
            files.append(entry)
            continue
        entry["path"] = label
        entry["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
        entry["bytes"] = len(raw)
        try:
            merged = deep_merge(merged, _load_documents(raw))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        files.append(entry)

    section: dict = {"source": "configured_in_source", "files": files}
    llm = extract_llm(merged)
    if llm:
        section["llm"] = llm
    actuator = extract_actuator(merged)
    if actuator:
        section["actuator"] = actuator
    ai = _spring_ai(merged)
    if ai:
        section["spring_ai"] = redact(ai)
    return section


def config_disagreements(section: dict, backend: dict) -> list[str]:
    """Fields where the configuration file and the running backend disagree.

    This is the check the section exists to make possible. A backend serving a
    model its own source does not name means the deployment is not running what
    the repository says, and every conclusion drawn from that source is suspect,
    including the fields the meter cannot verify.
    """
    declared = (section or {}).get("llm") or {}
    out = []
    for field in OPTION_FIELDS.values():
        said, seen = declared.get(field), (backend or {}).get(field)
        if not said or not seen:
            continue
        values = seen if isinstance(seen, list) else [seen]
        if not any(str(said) == str(v) for v in values):
            out.append(f"{field}: config file says {said!r}, backend reports {seen!r}")
    return out
