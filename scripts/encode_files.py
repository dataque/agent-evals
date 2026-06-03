#!/usr/bin/env python3
"""
Usage:
    python encode_files.py <input_folder> <output_folder> [options]

Options:
    --no-bundle       Encode files individually instead of as a
                      single compressed bundle (default is bundle
                      mode, which gives the best compression by
                      exploiting cross-file redundancy).
    --only-ext .ts    Only encode files with the given extension(s).
                      Comma-separate multiple: --only-ext .ts,.tsx
                      Useful for re-sending files that were previously
                      skipped or missed without re-encoding everything.

Modes (selected automatically unless overridden):
    Bundle (default)
        All files are tarred, brotli-compressed, and split
        into a single sequential stream of QR codes. Produces
        ~20-30% fewer QRs than per-file mode for source trees.
        Decoded with: decode_batch.py then assemble.py.

    Per-file (--no-bundle)
        Each file is brotli-compressed and encoded separately.
        Already-compressed files (.zip, .jar, etc.) skip
        compression. Useful when you need to transfer only a
        subset of files or when streaming incrementally.

    Delta (automatic)
        If qr_sync_state.json exists from a prior run, only
        files changed since the last sync SHA are encoded.
        Works in both bundle and per-file modes.

Examples:
    python encode_files.py ./my_project ./backup
    python encode_files.py ./my_project ./backup --no-bundle

Requirements:
    pip install qrcode pillow brotli
=============================================================
"""

import os
import sys
import io
import json
import math
import zlib
import base64
import tarfile
import subprocess
import brotli
import qrcode
from datetime import datetime, timezone
from pathlib import Path


# --- Configuration ---
MAX_CHARS_PER_QR = 1500
ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_M

# Extensions whose contents are already compressed — skip zlib for these
ALREADY_COMPRESSED_EXTS = {
    ".gz", ".bz2", ".xz", ".zst", ".br", ".lz4",
    ".zip", ".7z", ".rar", ".tar",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp3", ".mp4", ".mkv", ".avi", ".mov",
    ".jar", ".war", ".ear", ".apk",
    ".woff", ".woff2", ".pdf",
}

# Known text-based extensions
TEXT_EXTENSIONS = {
    # Programming languages
    ".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".cc",
    ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt",
    ".kts", ".scala", ".pl", ".pm", ".r", ".m", ".mm", ".lua", ".dart",
    ".groovy", ".clj", ".cljs", ".erl", ".ex", ".exs", ".hs", ".elm",
    ".v", ".vhdl", ".sv", ".zig", ".nim", ".cr", ".jl", ".f90", ".f95",
    # Web / markup / config
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".xml", ".xsl",
    ".xsd", ".dtd", ".svg", ".vue", ".svelte", ".astro",
    # Data / config
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".plist", ".hcl", ".tf", ".tfvars",
    # Shell / scripts
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".psm1",
    # Documentation / text
    ".txt", ".md", ".mdx", ".rst", ".tex", ".adoc", ".org", ".wiki",
    ".csv", ".tsv", ".log",
    # Database / query
    ".sql", ".graphql", ".gql", ".cql",
    # Build / project files
    ".gradle", ".cmake", ".mk", ".makefile", ".dockerfile",
    ".gitignore", ".gitattributes", ".editorconfig", ".eslintrc",
    ".prettierrc", ".babelrc",
    # Misc
    ".proto", ".thrift", ".avsc", ".lock", ".snap", ".patch", ".diff",
}

# Image, video, and audio files — skip entirely (not worth encoding as QR)
SKIP_MEDIA_EXTS = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
    ".tiff", ".tif", ".ico", ".raw", ".heic", ".heif", ".avif",
    # Video
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".3gp", ".m2ts",
    # Audio
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
    ".opus", ".aiff", ".aif",
}

# Files to always skip
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# Directories to always skip
SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", ".tox",
    ".venv", "venv", ".env", "env", ".idea", ".vscode", ".settings",
    "build", "dist", "target", ".gradle", ".next", ".nuxt",
    "*.egg-info", ".mypy_cache", ".pytest_cache", ".cache",
}

SYNC_STATE_FILE = Path(__file__).parent / "qr_sync_state.json"


def create_qr_image(data: str, filepath: str):
    """Generate a QR code image from a string and save it."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECTION,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filepath)


def is_text_file(filepath: Path) -> bool:
    """Determine if a file is text-based."""
    if filepath.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if filepath.name.lower() in {
        "makefile", "dockerfile", "vagrantfile", "gemfile",
        "rakefile", "procfile", "readme", "license", "changelog",
        "authors", "contributors", "todo", "copying",
    }:
        return True
    try:
        data = filepath.read_bytes()[:8192]
        data.decode("utf-8")
        if b"\x00" in data:
            return False
        return True
    except (UnicodeDecodeError, PermissionError):
        return False


def should_skip_file(filepath: Path) -> bool:
    if filepath.name in SKIP_NAMES:
        return True
    if filepath.suffix.lower() in SKIP_MEDIA_EXTS:
        return True
    return False


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS or dirname.startswith(".")


def safe_qr_stem(rel_path: str) -> str:
    """
    Create a unique QR filename stem from a relative path.

    'src/main/App.java'  → 'src__main__App_java'
    'test/App.java'      → 'test__App_java'
    'config.yaml'        → 'config_yaml'
    """
    safe = rel_path.replace("/", "__").replace("\\", "__")
    if "." in safe.split("__")[-1]:
        parts = safe.rsplit(".", 1)
        safe = f"{parts[0]}_{parts[1]}"
    return safe


def collect_files_recursive(input_dir: Path) -> list[tuple[Path, str]]:
    """
    Recursively collect all files, skipping unwanted directories.
    Returns list of (absolute_path, relative_path_string) tuples.
    """
    files = []
    skipped_media = 0
    for root, dirs, filenames in os.walk(input_dir):
        dirs[:] = sorted(d for d in dirs if not should_skip_dir(d))
        root_path = Path(root)
        for fname in sorted(filenames):
            fpath = root_path / fname
            if fpath.suffix.lower() in SKIP_MEDIA_EXTS:
                skipped_media += 1
                continue
            if should_skip_file(fpath):
                continue
            rel_path = fpath.relative_to(input_dir).as_posix()
            files.append((fpath, rel_path))
    if skipped_media:
        print(f"  ⏭ Skipped {skipped_media} media file(s) (image/video/audio)")
    return files


# --- Sync state helpers ---

def load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        return json.loads(SYNC_STATE_FILE.read_text())
    return {"repos": {}}


def save_sync_state(state: dict):
    SYNC_STATE_FILE.write_text(json.dumps(state, indent=2))


def get_repo_key(input_dir: Path) -> str:
    """Derive a stable repo identifier from the git remote URL, e.g. 'savipk/juno-be'."""
    try:
        result = subprocess.run(
            ["git", "-C", str(input_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            url = result.stdout.strip().rstrip("/")
            if url.endswith(".git"):
                url = url[:-4]
            # SSH: git@github.com:owner/repo  → owner/repo
            if "@" in url and ":" in url:
                url = url.split(":", 1)[1]
            # HTTPS: https://github.com/owner/repo → owner/repo
            elif "://" in url:
                url = url.split("://", 1)[1]
                parts = url.split("/", 1)
                url = parts[1] if len(parts) > 1 else url
            parts = url.strip("/").split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
            return parts[-1] if parts else input_dir.resolve().name
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return input_dir.resolve().name


def get_repo_syncs(state: dict, repo_key: str) -> list:
    return state["repos"].setdefault(repo_key, {"syncs": []})["syncs"]


def get_current_sha(input_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(input_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_delta_files(input_dir: Path, baseline_sha: str, target_sha: str = "HEAD") -> tuple[set[str], list[str]]:
    """
    Returns (changed_paths, deleted_paths) since baseline_sha up to target_sha.
    changed_paths: A/M/R-new paths to encode, relative to input_dir.
    deleted_paths: D/R-old paths to report in deletions.txt, relative to input_dir.

    git always reports paths relative to the repo root, so we resolve the repo root,
    compute the prefix (e.g. "folder-name/"), filter out paths outside input_dir,
    and strip the prefix so paths match what collect_files_recursive returns.
    """
    # Resolve repo root so diff paths are consistent regardless of input_dir depth
    root_result = subprocess.run(
        ["git", "-C", str(input_dir), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=10,
    )
    repo_root = Path(root_result.stdout.strip())

    # Prefix to filter and strip (empty string when input_dir IS the repo root)
    try:
        rel = input_dir.resolve().relative_to(repo_root.resolve()).as_posix()
        path_prefix = "" if rel == "." else rel + "/"
    except ValueError:
        path_prefix = ""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-status", f"{baseline_sha}..{target_sha}"],
        capture_output=True, text=True, timeout=30,
    )
    changed: set[str] = set()
    deleted: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0][0]  # first char handles R100, M100, etc.
        if status in ("A", "M"):
            path = parts[1]
            if path_prefix and not path.startswith(path_prefix):
                continue
            changed.add(path[len(path_prefix):])
        elif status == "D":
            path = parts[1]
            if path_prefix and not path.startswith(path_prefix):
                continue
            deleted.append(path[len(path_prefix):])
        elif status == "R":
            old, new = parts[1], parts[2]
            if not path_prefix or old.startswith(path_prefix):
                deleted.append(old[len(path_prefix):])
            if not path_prefix or new.startswith(path_prefix):
                changed.add(new[len(path_prefix):])
    return changed, deleted


def find_pending_sync(state: dict, repo_key: str) -> dict | None:
    for entry in reversed(get_repo_syncs(state, repo_key)):
        if not entry.get("completed", False):
            return entry
    return None


def prompt_pending(pending: dict) -> str:
    sha_short = pending["sha"][:12]
    encoded_at = pending.get("encoded_at", "unknown")
    files_encoded = pending.get("files_encoded", "?")
    print()
    print(f"  ⚠  Previous encoding not marked complete.")
    print(f"     SHA: {sha_short}  |  Encoded: {encoded_at}  |  QR files: {files_encoded}")
    print()
    print("  [g] Retry       — re-encode the failed batch (decoder is still at prior SHA)")
    print("  [m] Mark complete + encode delta (decoder already applied the prior batch)")
    print("  [a] Abort")
    print()
    while True:
        choice = input("  Choice [g/m/a]: ").strip().lower()
        if choice in ("g", "m", "a"):
            return choice
        print("  Please enter g, m, or a.")


def create_sync_state_qr(repo_key: str, sha: str, encoded_at: str, files_encoded: int, output_dir: Path):
    """Generate a sync state QR code as the final QR in the batch."""
    payload = json.dumps({
        "type": "sync_state",
        "repo": repo_key,
        "sha": sha,
        "encoded_at": encoded_at,
        "files_encoded": files_encoded,
    })
    qr_path = output_dir / "__sync_state__.png"
    try:
        create_qr_image(payload, str(qr_path))
        print(f"  🔖 __sync_state__.png (repo={repo_key}, sha={sha[:12]})")
    except Exception as e:
        print(f"  ⚠ Failed to create sync state QR: {e}")


def encode_file_to_qr_codes(filepath: Path, rel_path: str, output_dir: Path) -> int:
    """
    Read a file and produce one or more QR code images.

    Each QR code contains a JSON envelope:
    {
        "path": "src/main/App.java",
        "part": 1,
        "total_parts": 3,
        "encoding": "zlib+b85" | "b85",
        "content": "...base85-encoded chunk..."
    }
    """
    raw = filepath.read_bytes()
    skip_compression = filepath.suffix.lower() in ALREADY_COMPRESSED_EXTS
    compressed = raw if skip_compression else brotli.compress(raw, quality=11)
    if len(compressed) >= len(raw):  # compression didn't help
        compressed = raw
        skip_compression = True
    content = base64.b85encode(compressed).decode("ascii")
    encoding_type = "b85" if skip_compression else "brotli+b85"

    envelope_overhead = len(json.dumps({
        "path": rel_path,
        "part": 999,
        "total_parts": 999,
        "encoding": "brotli+b85",  # longest value — conservative estimate
        "content": ""
    }))

    usable_chars = MAX_CHARS_PER_QR - envelope_overhead - 20

    if usable_chars <= 0:
        print(f"  ⚠ Path too long for QR envelope, skipping: {rel_path}")
        return 0

    total_parts = max(1, math.ceil(len(content) / usable_chars))
    chunks = [content[i * usable_chars:(i + 1) * usable_chars] for i in range(total_parts)]

    qr_stem = safe_qr_stem(rel_path)
    generated = 0

    for i, chunk in enumerate(chunks):
        part_num = i + 1
        envelope = json.dumps({
            "path": rel_path,
            "part": part_num,
            "total_parts": total_parts,
            "encoding": encoding_type,
            "content": chunk
        }, ensure_ascii=False)

        if total_parts == 1:
            qr_filename = f"{qr_stem}.png"
        else:
            qr_filename = f"{qr_stem}_part{part_num:03d}_of_{total_parts:03d}.png"

        qr_path = output_dir / qr_filename

        try:
            create_qr_image(envelope, str(qr_path))
            generated += 1
            print(f"  ✅ {qr_filename} ({len(chunk)} chars)")
        except Exception as e:
            print(f"  ❌ Failed to create {qr_filename}: {e}")

    return generated


def encode_bundle_to_qr_codes(files: list[tuple[Path, str]], output_dir: Path) -> int:
    """Tar all files, brotli-compress, base85-encode, split into QR chunks."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for filepath, rel_path in files:
            tar.add(filepath, arcname=rel_path)
    raw = buf.getvalue()

    compressed = brotli.compress(raw, quality=11)
    content = base64.b85encode(compressed).decode("ascii")

    overhead = len(json.dumps({"type": "bundle", "part": 9999, "total": 9999,
                               "encoding": "brotli+b85", "content": ""}))
    usable = MAX_CHARS_PER_QR - overhead - 20
    total = max(1, math.ceil(len(content) / usable))
    chunks = [content[i * usable:(i + 1) * usable] for i in range(total)]

    generated = 0
    for i, chunk in enumerate(chunks):
        part_num = i + 1
        envelope = json.dumps({"type": "bundle", "part": part_num, "total": total,
                               "encoding": "brotli+b85", "content": chunk})
        qr_path = output_dir / f"__bundle__chunk_{part_num:04d}_of_{total:04d}.png"
        try:
            create_qr_image(envelope, str(qr_path))
            generated += 1
            print(f"  ✅ {qr_path.name} ({len(chunk)} chars)")
        except Exception as e:
            print(f"  ❌ Failed to create {qr_path.name}: {e}")
    return generated


def generate_manifest_qrs(files: list[tuple[Path, str]], output_dir: Path):
    manifest_dir = output_dir / "__manifest__"
    manifest_dir.mkdir(exist_ok=True)

    paths = [rel for _, rel in files]
    raw = json.dumps(paths, separators=(",", ":")).encode("utf-8")
    compressed = brotli.compress(raw, quality=11)
    content = base64.b85encode(compressed).decode("ascii")

    overhead = len(json.dumps({"type": "manifest", "part": 999, "total": 999,
                               "encoding": "brotli+b85", "content": ""}))
    usable = MAX_CHARS_PER_QR - overhead - 20
    total = max(1, math.ceil(len(content) / usable))
    chunks = [content[i * usable:(i + 1) * usable] for i in range(total)]

    for i, chunk in enumerate(chunks):
        part_num = i + 1
        envelope = json.dumps({"type": "manifest", "part": part_num, "total": total,
                               "encoding": "brotli+b85", "content": chunk})
        qr_path = manifest_dir / f"manifest_part{part_num:04d}_of_{total:04d}.png"
        create_qr_image(envelope, str(qr_path))

    print(f"  📋 Manifest: {len(paths)} files → {total} QR(s) in {manifest_dir.name}/")


def main():
    if len(sys.argv) < 3:
        print("Usage: python encode_files.py <input_folder> <output_folder> [--no-bundle] [--only-ext .ts,.tsx,...]")
        print("Example: python encode_files.py ./my_project ./qr_codes")
        print("         python encode_files.py ./my_project ./qr_codes --no-bundle --only-ext .ts")
        sys.exit(1)

    bundle_mode = "--no-bundle" not in sys.argv
    if not bundle_mode:
        sys.argv.remove("--no-bundle")

    only_exts: set[str] | None = None
    if "--only-ext" in sys.argv:
        idx = sys.argv.index("--only-ext")
        only_exts = {e.strip().lower() for e in sys.argv[idx + 1].split(",")}
        sys.argv.pop(idx + 1)
        sys.argv.pop(idx)

    input_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not input_dir.is_dir():
        print(f"❌ Input folder not found: {input_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Sync state & delta detection ---
    state = load_sync_state()
    repo_key = get_repo_key(input_dir)

    # Migrate old flat format {"syncs": [...]} → {"repos": {repo_key: {"syncs": [...]}}}
    if "syncs" in state and "repos" not in state:
        print(f"  ⚠  Migrating sync state to multi-repo format (key: {repo_key})")
        state = {"repos": {repo_key: {"syncs": state["syncs"]}}}
        save_sync_state(state)

    current_sha = get_current_sha(input_dir)
    pending = find_pending_sync(state, repo_key)
    repo_syncs = get_repo_syncs(state, repo_key)
    baseline_sha = None
    target_sha = "HEAD"
    delta_mode = False
    is_retry = False

    if pending:
        choice = prompt_pending(pending)
        if choice == "a":
            print("  Aborted.")
            sys.exit(0)
        if choice == "m":
            pending["completed"] = True
            save_sync_state(state)
            print(f"  ✅ Marked SHA {pending['sha'][:12]} as complete.")
            baseline_sha = pending["sha"]
        else:  # choice == "g": retry the failed batch
            last_completed = next(
                (e for e in reversed(repo_syncs) if e.get("completed")), None
            )
            baseline_sha = last_completed["sha"] if last_completed else None
            target_sha = pending["sha"]
            is_retry = True
        delta_mode = True
    elif repo_syncs:
        # All prior syncs completed — use last one as delta baseline
        baseline_sha = repo_syncs[-1]["sha"]
        delta_mode = True

    # --- Collect files ---
    all_files = collect_files_recursive(input_dir)
    deleted_paths: list[str] = []

    if only_exts is not None:
        before = len(all_files)
        all_files = [(p, r) for (p, r) in all_files if p.suffix.lower() in only_exts]
        print(f"  🔍 --only-ext {','.join(sorted(only_exts))}: {len(all_files)} of {before} files match")

    if delta_mode and baseline_sha:
        changed_paths, deleted_paths = get_delta_files(input_dir, baseline_sha, target_sha)
        all_files = [(p, r) for (p, r) in all_files if r in changed_paths]

        if deleted_paths:
            deletions_file = output_dir / "deletions.txt"
            deletions_file.write_text("\n".join(deleted_paths) + "\n")
            print(f"\n  🗑  {len(deleted_paths)} deleted file(s) → {deletions_file}")
            print(f"     On decoder side: delete each path listed in deletions.txt\n")

        if not all_files and not deleted_paths:
            print(f"\n  ✅ No changes since last sync (SHA {baseline_sha[:12]})")
            if current_sha and current_sha != baseline_sha and not is_retry:
                entry = {
                    "sha": current_sha,
                    "encoded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    "output_dir": str(output_dir.resolve()),
                    "files_encoded": 0,
                    "completed": False,
                }
                repo_syncs.append(entry)
                save_sync_state(state)
                print(f"  SHA recorded: {current_sha[:12]} (run mark_complete.py after decoding)")
            sys.exit(0)

    if not all_files and not deleted_paths:
        print(f"❌ No files found in {input_dir}")
        sys.exit(1)

    text_files = [(p, r) for p, r in all_files if is_text_file(p)]
    binary_files = [(p, r) for p, r in all_files if not is_text_file(p)]

    unique_dirs = set()
    for _, rel in all_files:
        parent = str(Path(rel).parent)
        if parent != ".":
            unique_dirs.add(parent)

    print(f"{'='*60}")
    mode_label = "Bundle" if bundle_mode else ("Retry" if is_retry else ("Delta" if delta_mode else "Full"))
    print(f"  QR Code Generator — {mode_label} Mode")
    print(f"{'='*60}")
    print(f"  Repo:          {repo_key}")
    print(f"  Input folder:  {input_dir.resolve()}")
    print(f"  Output folder: {output_dir.resolve()}")
    if delta_mode and baseline_sha:
        print(f"  Baseline SHA:  {baseline_sha[:12]}")
        display_target = target_sha if target_sha != "HEAD" else current_sha
        if display_target:
            label = "Target SHA:  " if is_retry else "Current SHA: "
            print(f"  {label}  {display_target[:12]}")
    print(f"  Directories:   {len(unique_dirs) + 1} (including root)")
    print(f"  Text files:    {len(text_files)}")
    print(f"  Binary files:  {len(binary_files)} (base64 encoded)")
    print(f"  Total files:   {len(all_files)}")
    if deleted_paths:
        print(f"  Deletions:     {len(deleted_paths)} (see deletions.txt)")
    print(f"  Max chars/QR:  {MAX_CHARS_PER_QR}")
    print(f"{'='*60}")

    extensions = set(p.suffix.lower() for p, _ in all_files if p.suffix)
    if extensions:
        print(f"  Extensions:    {', '.join(sorted(extensions))}")
    if unique_dirs:
        print(f"  Subdirs:       {', '.join(sorted(unique_dirs))}")
    print()

    total_qr = 0
    skipped = 0

    if bundle_mode:
        print(f"  Bundling {len(all_files)} files into a single compressed stream...")
        print()
        total_qr = encode_bundle_to_qr_codes(all_files, output_dir)
    else:
        current_dir = None
        for filepath, rel_path in all_files:
            parent = str(Path(rel_path).parent)
            if parent != current_dir:
                current_dir = parent
                dir_label = parent if parent != "." else "(root)"
                print(f"📁 {dir_label}/")

            file_size = filepath.stat().st_size
            mode = "brotli+b85" if filepath.suffix.lower() not in ALREADY_COMPRESSED_EXTS else "b85"
            print(f"  📄 {rel_path} ({file_size:,} bytes) [{mode}]")
            count = encode_file_to_qr_codes(filepath, rel_path, output_dir)
            total_qr += count
            if count == 0:
                skipped += 1
            print()

    generate_manifest_qrs(all_files, output_dir)

    print(f"{'='*60}")
    print(f"  ✅ Done! Generated {total_qr} QR code(s) from {len(all_files)} file(s)")
    if skipped:
        print(f"  ⚠ Skipped {skipped} file(s)")
    print(f"  📁 Output: {output_dir.resolve()}")

    if current_sha and not is_retry:
        entry = {
            "sha": current_sha,
            "encoded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "output_dir": str(output_dir.resolve()),
            "files_encoded": total_qr,
            "completed": False,
        }
        repo_syncs.append(entry)
        save_sync_state(state)
        print(f"  SHA recorded: {current_sha[:12]} — run mark_complete.py <input_dir> after decoding")
    elif is_retry:
        print(f"  Retrying batch for SHA {target_sha[:12]} — run mark_complete.py <input_dir> after decoding")

    print(f"{'='*60}")
    print()
    print("NEXT STEPS:")


if __name__ == "__main__":
    main()
