"""Read the system-under-test's own Spring configuration file.

E19's third source, and the only one that can answer reasoning effort on a
deployment like ours.

* The GenAI meter (``transport/actuator.py``) says what the backend *did*: it is
  a record of calls that actually happened, but a Micrometer meter carries only
  the convention's low-cardinality tags, so it can never carry a request option.
* The actuator's ``configprops`` / ``env`` endpoints *do* carry request options,
  but they are commonly not exposed. On the deployment this was written against,
  ``/actuator`` publishes exactly ``health`` and ``metrics``, so that route is
  closed permanently rather than transiently.
* The backend's ``application.yaml`` carries them plainly, and when the eval runs
  beside the backend the file is simply readable.

This lands in its own ``params.json`` section rather than being merged into
``backend`` **on purpose**. The file says what the build was *configured* with;
the meter says what the process *actually did*. Those two disagreeing is a
finding worth seeing, not a conflict to resolve: on 2026-08-25 the file declared
``model: gpt-5.2`` while the meter reported ``gpt-5.5`` across 89 real calls.
Merging them would have hidden exactly the drift E19 exists to catch.

Nothing here touches the environment. There is no new ``AGENT_EVALS_*`` variable,
and the backend's own ``${...}`` placeholders are recorded verbatim rather than
resolved against the eval's environment, which would invent a value the backend
never saw.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# Spring property names (normalised: lower-cased, punctuation stripped) worth
# lifting to the top of the section. `reasoning-effort`, `reasoningEffort` and
# `REASONING_EFFORT` all normalise to the same key.
_MODEL_FIELDS = {
    "model": "model",
    "reasoningeffort": "reasoning_effort",
    "deploymentname": "deployment",
    "temperature": "temperature",
    "maxtokens": "max_tokens",
    "maxcompletiontokens": "max_completion_tokens",
    "topp": "top_p",
    "verbosity": "verbosity",
    "timeout": "timeout",
    "maxretries": "max_retries",
    "baseurl": "base_url",
    "apiversion": "api_version",
    "serviceversion": "api_version",
}

# A value under a key like this is never recorded. `${VAR}` placeholders are
# exempt: they name where the secret comes from without disclosing it, which is
# provenance rather than a leak.
# Sibling modalities that carry their own `model`. The agent's LLM is the chat
# one; an embedding model in the provenance block would be simply wrong.
_NON_CHAT_MODALITIES = {"embedding", "embeddings", "image", "audio", "speech",
                        "transcription", "moderation", "vectorstore"}

_SECRET_KEY = re.compile(r"key|secret|password|token|credential", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^\$\{[^}]*\}$")
_REDACTED = "<redacted>"

# Bounded globs instead of a recursive walk: a service monorepo holds a great
# many application.yaml files and only one of them configures the LLM.
_GLOBS = (
    "application.yaml", "application.yml",
    "*/src/main/resources/application.yaml",
    "*/*/src/main/resources/application.yaml",
    "*/*/*/src/main/resources/application.yaml",
    "*/src/main/resources/application.yml",
    "*/*/src/main/resources/application.yml",
    "*/*/*/src/main/resources/application.yml",
)


def _normalise(key: object) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _load_docs(path: Path) -> list[dict]:
    """Every YAML document in the file. Spring allows several per file, each
    optionally gated on a profile."""
    with path.open(encoding="utf-8") as fh:
        return [doc for doc in yaml.safe_load_all(fh) if isinstance(doc, dict)]


def _doc_profile(doc: dict) -> str | None:
    activate = ((doc.get("spring") or {}).get("config") or {}).get("activate") or {}
    value = activate.get("on-profile")
    return str(value) if value else None


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _redact(node: Any, key: object = "") -> Any:
    if isinstance(node, dict):
        return {k: _redact(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(v, key) for v in node]
    text = str(node) if node is not None else ""
    if _SECRET_KEY.search(str(key)) and not _PLACEHOLDER.match(text.strip()):
        return _REDACTED
    return node


def _collect_model_fields(node: Any, out: dict, *, in_chat: bool = False) -> None:
    """Sweep the ``spring.ai`` subtree for the CHAT model's settings.

    Scoped to chat on purpose. A service that also configures embeddings has a
    ``model`` under ``spring.ai.openai.embedding`` too, and reading that as the
    agent's LLM would put ``text-embedding-3-large`` in the run's provenance.
    Settings are read from inside a ``chat`` block, or from the client block that
    owns one (``base-url``, ``timeout`` and ``max-retries`` live there).

    Shallower keys win: Spring AI exposes both ``chat.model`` and
    ``chat.options.model`` for the same setting, and the flattened form is the
    one these configs are written with.
    """
    if isinstance(node, list):
        for item in node:
            _collect_model_fields(item, out, in_chat=in_chat)
        return
    if not isinstance(node, dict):
        return
    owns_chat = any(_normalise(key) == "chat" for key in node)
    if in_chat or owns_chat:
        for key, value in node.items():
            field = _MODEL_FIELDS.get(_normalise(key))
            if field and field not in out and not isinstance(value, (dict, list)):
                text = str(value).strip() if value is not None else ""
                if text and text != _REDACTED:
                    out[field] = value
    for key, value in node.items():
        if not isinstance(value, (dict, list)):
            continue
        normalised = _normalise(key)
        if normalised in _NON_CHAT_MODALITIES:
            continue
        _collect_model_fields(value, out, in_chat=in_chat or normalised == "chat")


def find_application_yaml(search_from: Path, explicit: str | list[str] | None = None) -> list[Path]:
    """Candidate Spring config files, best first.

    An explicit path is taken at its word. Otherwise the working directory and
    its parent are swept, and candidates are ranked by whether they actually
    configure an LLM: a monorepo has one ``application.yaml`` per service and
    only one of them mentions ``spring.ai``.
    """
    if explicit:
        paths = [explicit] if isinstance(explicit, str) else list(explicit)
        return [Path(p).expanduser() for p in paths]

    seen: list[Path] = []
    for root in (search_from, search_from.parent):
        for pattern in _GLOBS:
            try:
                matches = sorted(root.glob(pattern))
            except OSError:
                continue
            for match in matches:
                if match.is_file() and match not in seen:
                    seen.append(match)
    # only the ones that configure a model, so a monorepo's other services and
    # the eval's own config cannot be mistaken for the backend's
    return [p for p in seen if _mentions_spring_ai(p)]


def _mentions_spring_ai(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "spring:" in text and "ai:" in text and "model" in text


def read_backend_config(*, path: str | list[str] | None = None,
                        profiles: list[str] | None = None,
                        search_from: Path | None = None) -> dict:
    """The backend's declared model settings, for ``params.json``. Never raises.

    Returns the flattened model fields, the ``spring.ai`` subtree it read them
    from (secrets redacted), and which files and profiles were applied. On
    failure it returns a block naming what was searched, so "the harness looked
    and found nothing" stays distinguishable from "the harness never looked".
    """
    root = search_from or Path.cwd()
    wanted = [str(p) for p in (profiles or [])]
    try:
        candidates = find_application_yaml(root, path)
    except Exception as exc:  # noqa: BLE001 - provenance must not fail a run
        return {"error": f"{type(exc).__name__}: {exc}"}

    chosen: dict | None = None
    conflicting: list[dict] = []
    for candidate in candidates:
        try:
            merged, files, applied = _read_one(candidate, wanted)
        except Exception:  # noqa: BLE001 - a malformed sibling must not stop the search
            continue
        spring_ai = ((merged.get("spring") or {}).get("ai")) or {}
        if not isinstance(spring_ai, dict) or not spring_ai:
            continue
        redacted = _redact(spring_ai)
        fields: dict = {}
        _collect_model_fields(redacted, fields)
        if not fields:
            continue
        if chosen is not None:
            # A monorepo points every service at the same LLM, so most other
            # matches agree and are noise. One that DISAGREES is the real risk:
            # it means alphabetical order picked which answer this run records.
            if _disagrees(_identity(fields), _identity(chosen)):
                conflicting.append({"path": str(candidate), **_identity(fields)})
            continue
        chosen = {**fields, "spring_ai": redacted, "path": str(candidate), "files": files}
        name = ((merged.get("spring") or {}).get("application") or {}).get("name")
        if name:
            # WHICH service was read. A monorepo has many, and only one is the
            # system under test.
            chosen["application_name"] = str(name)
        if applied:
            chosen["profiles_applied"] = applied
        siblings = _profile_files(candidate)
        if siblings:
            chosen["profile_files_available"] = siblings
        chosen["source"] = "application.yaml"

    if chosen is None:
        return {"error": "no application.yaml with spring.ai model settings found",
                "searched": [str(root), str(root.parent)]}
    if conflicting:
        chosen["conflicting_candidates"] = conflicting
    return chosen


def _identity(fields: dict) -> dict:
    """The fields that say WHICH model, ignoring transport settings like timeout
    that every service shares and that say nothing about model identity."""
    return {k: v for k, v in fields.items()
            if k in ("model", "deployment", "reasoning_effort") and v is not None}


def _disagrees(a: dict, b: dict) -> bool:
    """Whether two configs actually contradict each other.

    Only shared keys count. One service spelling out a ``deployment`` that
    another leaves implicit is different completeness, not a different model, and
    reporting it as a conflict would bury the case that matters.
    """
    return any(a[key] != b[key] for key in set(a) & set(b))


def _read_one(base: Path, profiles: list[str]) -> tuple[dict, list[str], list[str]]:
    """Merge the base file, its profile-gated documents, and any requested
    ``application-<profile>.yaml`` siblings, in Spring's precedence order."""
    merged: dict = {}
    files = [base.name]
    applied: list[str] = []
    for doc in _load_docs(base):
        gate = _doc_profile(doc)
        if gate and gate not in profiles:
            continue
        if gate:
            applied.append(gate)
        merged = _deep_merge(merged, doc)
    for profile in profiles:
        sibling = base.with_name(f"{base.stem}-{profile}{base.suffix}")
        if not sibling.is_file():
            continue
        files.append(sibling.name)
        applied.append(profile)
        for doc in _load_docs(sibling):
            merged = _deep_merge(merged, doc)
    return merged, files, sorted(set(applied))


def _profile_files(base: Path) -> list[str]:
    """Profile overlays sitting next to the base file but NOT applied, so the
    operator can see what else exists without the harness guessing which is
    active."""
    try:
        found = sorted(p.name for p in base.parent.glob(f"{base.stem}-*{base.suffix}"))
    except OSError:
        return []
    return found
