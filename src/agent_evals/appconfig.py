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

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any

import yaml

# Where a Spring Boot fat jar keeps the config it was built with. Reading the JAR
# is closer to the truth than reading a source tree: the jar is what the pod
# actually started, while the checkout beside it may have moved on since.
_JAR_CLASSES = "BOOT-INF/classes/"
_BASE_NAMES = ("application.yaml", "application.yml")

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

# Built artefacts, swept BEFORE the source tree. A jar is what the pod actually
# started; the checkout beside it may have been synced or edited since, and this
# whole section exists because a deployment can stop matching its own source.
_JAR_GLOBS = (
    "*.jar",
    "*/*.jar",
    "*/target/*.jar",
    "*/*/target/*.jar",
    "*/*/*/target/*.jar",
)


def _normalise(key: object) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _load_docs(raw: bytes) -> list[dict]:
    """Every YAML document in one config file. Spring allows several per file,
    each optionally gated on a profile."""
    text = raw.decode("utf-8", errors="replace")
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def _is_jar(path: Path) -> bool:
    return path.suffix.lower() == ".jar"


def _jar_config_names(path: Path) -> list[str]:
    """Config members of a Spring Boot fat jar, base file first."""
    try:
        with zipfile.ZipFile(path) as jar:
            names = jar.namelist()
    except Exception:  # noqa: BLE001 - not a readable jar is simply not a candidate
        return []
    prefix = _JAR_CLASSES
    return sorted(n for n in names
                  if n.startswith(prefix)
                  and re.fullmatch(r"application(-[^/]+)?\.ya?ml", n[len(prefix):] or ""))


def _read_member(path: Path, member: str | None) -> bytes:
    if member is None:
        return path.read_bytes()
    with zipfile.ZipFile(path) as jar:
        return jar.read(member)


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


# The service actually under test. A backend monorepo has many services and
# most of them configure an LLM, so "declares a chat model" is not enough to
# identify the one the eval is talking to. The eval's endpoint is
# `/api/v1/bff/ai/agent/sse`, so the BFF is the service that answers.
DEFAULT_SERVICE_HINT = "bff"


def service_name(path: Path) -> str:
    """The service a config file belongs to, from its position in the tree.

    ``<repo>/<group>/bff-service/src/main/resources/application.yaml`` and
    ``<repo>/<group>/bff-service/target/bff-service-0.1.0.jar`` both belong to
    ``bff-service``: the directory holding ``src`` or ``target``.

    Derived from that one component rather than the whole path on purpose. An
    ancestor directory can easily contain the hint (a workspace folder, a repo
    named after the product, a temp directory named after the test) and matching
    the full string would then match every candidate and discriminate nothing.
    """
    parts = [p.lower() for p in path.parts]
    for marker in ("src", "target"):
        if marker in parts:
            index = parts.index(marker)
            if index > 0:
                return parts[index - 1]
    return path.stem.lower() if _is_jar(path) else path.parent.name.lower()


def _rank(path: Path, hint: str) -> tuple:
    """Sort key for candidates, best first.

    Service identity outranks artefact type: the WRONG service's jar is worse
    provenance than the RIGHT service's source, because it names a model that
    never answered a single turn of this run.
    """
    return (0 if hint and hint in service_name(path) else 1,  # the service under test
            0 if _is_jar(path) else 1,                        # then what the pod started
            # component-wise, so `backend/` sorts before `backend-baseline/` as
            # pathlib orders them; a raw string compare puts '-' before '/'.
            tuple(p.lower() for p in path.parts))


def find_application_yaml(search_from: Path, explicit: str | list[str] | None = None,
                          root: str | None = None, service: str | None = None) -> list[Path]:
    """Candidate Spring config files, best first.

    An explicit path is taken at its word. Given a ``root``, only that tree is
    swept, which is how a workspace holding several backend checkouts (a working
    copy, a restored copy, an eval baseline) is kept from deciding the answer by
    alphabetical accident. With neither, the working directory and its parent are
    swept, since the eval runs beside the backend.

    Candidates must actually configure a chat model, and are then ranked by
    ``service`` (default ``bff``) before artefact type.
    """
    if explicit:
        paths = [explicit] if isinstance(explicit, str) else list(explicit)
        return [Path(p).expanduser() for p in paths]

    if root:
        roots = [Path(root).expanduser()]
    else:
        roots = [search_from, search_from.parent]

    seen: list[Path] = []
    for patterns in (_JAR_GLOBS, _GLOBS):
        for base in roots:
            for pattern in patterns:
                try:
                    matches = sorted(base.glob(pattern))
                except OSError:
                    continue
                for match in matches:
                    if match.is_file() and match not in seen:
                        seen.append(match)
    hint = (service or DEFAULT_SERVICE_HINT).strip().lower()
    # only the ones that configure a model, so a monorepo's other services and
    # the eval's own config cannot be mistaken for the backend's
    candidates = [p for p in seen if _mentions_spring_ai(p)]
    return sorted(candidates, key=lambda p: _rank(p, hint))


def _mentions_spring_ai(path: Path) -> bool:
    if _is_jar(path):
        names = _jar_config_names(path)
        primary = next((n for n in names if n[len(_JAR_CLASSES):] in _BASE_NAMES), None)
        if primary is None:
            return False
        try:
            text = _read_member(path, primary).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return False
        return "spring" in text and "ai" in text and "model" in text
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "spring:" in text and "ai:" in text and "model" in text


def read_backend_config(*, path: str | list[str] | None = None,
                        backend_root: str | None = None,
                        service: str | None = None,
                        profiles: list[str] | None = None,
                        search_from: Path | None = None) -> dict:
    """The backend's declared model settings, for ``params.json``. Never raises.

    ``backend_root`` points at the backend repo, which is the reliable way to say
    WHICH checkout to read in a workspace holding more than one. ``service``
    (default ``bff``) says which service inside it answers the eval's endpoint.

    Returns the flattened model fields, the ``spring.ai`` subtree it read them
    from (secrets redacted), and which files and profiles were applied. On
    failure it returns a block naming what was searched, so "the harness looked
    and found nothing" stays distinguishable from "the harness never looked".
    """
    root = search_from or Path.cwd()
    wanted = [str(p) for p in (profiles or [])]
    backend_root = (backend_root or "").strip() or None
    hint = (service or DEFAULT_SERVICE_HINT).strip().lower()
    if backend_root and not Path(backend_root).expanduser().is_dir():
        # A root that does not exist is a configuration mistake, and silently
        # sweeping the workspace instead would hide it behind a plausible answer
        # read from the wrong repository.
        return {"error": f"backend root not found: {backend_root}",
                "backend_root": backend_root}
    try:
        candidates = find_application_yaml(root, path, root=backend_root, service=hint)
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
        chosen = {**fields, "spring_ai": redacted, "path": str(candidate),
                  # A jar is the artefact the pod started; a source tree is what
                  # someone hopes it started. Worth stating which one answered.
                  "kind": "jar" if _is_jar(candidate) else "source",
                  "files": files}
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
        if backend_root:
            chosen["backend_root"] = backend_root
        chosen["service_hint"] = hint
        if hint and hint not in service_name(candidate):
            # Read SOMETHING, but not the service under test. Silence here is how
            # a run ends up citing another service's model as its own.
            chosen["service_hint_unmatched"] = True

    if chosen is None:
        searched = [backend_root] if backend_root else [str(root), str(root.parent)]
        return {"error": "no application.yaml with spring.ai model settings found",
                "searched": searched,
                **({"backend_root": backend_root} if backend_root else {}),
                "service_hint": hint}
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


def _overlay_sources(base: Path, profiles: list[str]) -> list[tuple[str, Path, str | None]]:
    """``(display name, file to read, jar member)`` for each requested profile
    overlay, in the order Spring would layer them.

    A plain overlay lives in its own file beside the base, so it carries its own
    path; a jar overlay is another member of the same archive.
    """
    out: list[tuple[str, Path, str | None]] = []
    if _is_jar(base):
        available = {name[len(_JAR_CLASSES):]: name for name in _jar_config_names(base)}
        for profile in profiles:
            for suffix in ("yaml", "yml"):
                name = f"application-{profile}.{suffix}"
                if name in available:
                    out.append((name, base, available[name]))
                    break
        return out
    for profile in profiles:
        sibling = base.with_name(f"{base.stem}-{profile}{base.suffix}")
        if sibling.is_file():
            out.append((sibling.name, sibling, None))
    return out


def _read_one(base: Path, profiles: list[str]) -> tuple[dict, list[dict], list[str]]:
    """Merge the base config, its profile-gated documents, and any requested
    profile overlays, in Spring's precedence order.

    Works the same for a source tree and for a Spring Boot fat jar; only where
    the bytes come from differs. Each file read is recorded with a digest, so two
    runs can be shown to have been produced by the same configuration without
    trusting the values to have been copied correctly.
    """
    merged: dict = {}
    files: list[dict] = []
    applied: list[str] = []

    if _is_jar(base):
        names = _jar_config_names(base)
        primary = next((n for n in names
                        if n[len(_JAR_CLASSES):] in _BASE_NAMES), None)
        if primary is None:
            raise FileNotFoundError(f"no {_JAR_CLASSES}application.yaml in {base}")
        sources: list[tuple[str, Path, str | None]] = [
            (primary[len(_JAR_CLASSES):], base, primary)]
    else:
        sources = [(base.name, base, None)]
    sources += _overlay_sources(base, profiles)

    for index, (name, source_path, member) in enumerate(sources):
        raw = _read_member(source_path, member)
        files.append({"name": name, "digest": _digest(raw), "bytes": len(raw)})
        if index:  # an overlay is applied wholesale; only the base is gated
            applied.append(name.rsplit(".", 1)[0].split("-", 1)[-1])
        for doc in _load_docs(raw):
            gate = _doc_profile(doc)
            if gate and gate not in profiles:
                continue
            if gate:
                applied.append(gate)
            merged = _deep_merge(merged, doc)
    return merged, files, sorted(set(applied))


def _profile_files(base: Path) -> list[str]:
    """Profile overlays sitting beside the base config but NOT applied, so the
    operator can see what else exists without the harness guessing which is
    active."""
    if _is_jar(base):
        return [n[len(_JAR_CLASSES):] for n in _jar_config_names(base)
                if n[len(_JAR_CLASSES):] not in _BASE_NAMES]
    try:
        return sorted(p.name for p in base.parent.glob(f"{base.stem}-*{base.suffix}"))
    except OSError:
        return []
