"""Real file operations. No simulation — every tool touches the actual FS,
with path-traversal protection and size caps."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from .base import PermissionLevel, ToolContext, ToolError, ToolResult, ToolSpec, resolve_safe_path

MAX_READ_BYTES = 2 * 1024 * 1024        # 2 MB per file read
MAX_TEXT_BYTES = 4 * 1024 * 1024        # 4 MB text scan per file for grep
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_GREP_RESULTS = 200
MAX_ARCHIVE_MEMBERS = 5000
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".xml",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".sql", ".db", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".lua", ".r",
    ".jl", ".dart", ".vue", ".svelte", ".log", ".env", ".gitignore", ".dockerfile", ".license",
    ".svg", ".ipynb", ".rst", ".tex", ".nix",
}
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".docx",
                     ".xlsx", ".pptx", ".zip", ".gz", ".tar", ".7z", ".rar", ".exe", ".dll",
                     ".so", ".dylib", ".bin", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav"}


def _detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


async def _read_file(ctx: ToolContext, path: str, max_bytes: int = MAX_READ_BYTES,
                     offset: int = 0) -> tuple[str, str]:
    real = resolve_safe_path(path, must_exist=True)
    size = os.path.getsize(real)
    if size > MAX_READ_BYTES * 20:
        raise ToolError(f"file too large to read safely ({size} bytes): {path}", kind="invalid")
    with open(real, "rb") as fh:
        if offset:
            fh.seek(offset)
        data = fh.read(max_bytes)
    enc = _detect_encoding(data)
    try:
        text = data.decode(enc)
    except UnicodeDecodeError:
        raise ToolError(f"file is binary or not decodable ({enc}): {path}", kind="invalid") from None
    return real, text


def register_file_tools(registry) -> None:
    # ------------------------------------------------------------- read
    async def read_file(ctx: ToolContext, path: str, offset: int = 0, max_bytes: int = MAX_READ_BYTES) -> ToolResult:
        real, text = await _read_file(ctx, path, max_bytes=max_bytes, offset=offset)
        size = os.path.getsize(real)
        truncated = len(text.encode("utf-8", "replace")) < size - offset
        head = text[: 40_000]
        if size - offset > len(head.encode("utf-8", "replace")):
            head += f"\n\n… [truncated: {size - offset} bytes total; showing first {len(head)} chars. Use offset to page.]"
        return ToolResult.ok(
            f"--- {real} ({size} bytes) ---\n{head}",
            data={"path": real, "size": size, "encoding": "utf-8", "truncated": truncated,
                  "offset": offset},
        )

    registry.register(ToolSpec(
        name="read_file", description="Read a text file (with optional byte offset for paging).",
        purpose="Inspect file contents", category="files",
        parameters={"path": {"type": "string", "required": True, "description": "Absolute or workspace-relative path"},
                    "offset": {"type": "integer", "minimum": 0, "description": "Byte offset to start reading from"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES}},
        handler=read_file, permission=PermissionLevel.READ_ONLY, timeout=20,
    ))

    # ------------------------------------------------------------- list
    async def list_directory(ctx: ToolContext, path: str = ".", max_entries: int = 200) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        if not os.path.isdir(real):
            raise ToolError(f"not a directory: {path}", kind="invalid")
        entries = []
        for name in sorted(os.listdir(real))[: max(1, min(max_entries, MAX_LIST_ENTRIES))]:
            full = os.path.join(real, name)
            try:
                st = os.stat(full)
                entries.append({
                    "name": name + ("/" if os.path.isdir(full) else ""),
                    "size": st.st_size if os.path.isfile(full) else None,
                    "modified": _fmt_ts(st.st_mtime),
                })
            except OSError:
                entries.append({"name": name})
        total = len(os.listdir(real))
        text = "\n".join(f"{e['name']:40} {e.get('size', '')} {e.get('modified', '')}" for e in entries)
        if total > len(entries):
            text += f"\n… {total - len(entries)} more entries"
        return ToolResult.ok(f"--- {real} ({total} entries) ---\n{text}",
                             data={"path": real, "entries": entries, "total": total})

    registry.register(ToolSpec(
        name="list_directory", description="List the contents of a directory.",
        purpose="Browse folders", category="files",
        parameters={"path": {"type": "string", "default": ".", "description": "Directory path"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_ENTRIES}},
        handler=list_directory, permission=PermissionLevel.READ_ONLY, timeout=15,
    ))

    # ------------------------------------------------------------- search
    async def search_files(ctx: ToolContext, pattern: str, path: str = ".", recursive: bool = True,
                           max_results: int = 100) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        rx = re.compile(re.escape(pattern).replace(r"\*", ".*").replace(r"\?", "."))
        hits: list[str] = []
        base = Path(real)
        iterator = base.rglob("*") if recursive else base.glob("*")
        for p in iterator:
            try:
                if p.is_file() and rx.search(p.name):
                    hits.append(str(p))
                    if len(hits) >= min(max_results, MAX_SEARCH_RESULTS):
                        break
            except OSError:
                continue
        if not hits:
            return ToolResult.ok(f"No files matching '{pattern}' under {real}",
                                 data={"path": real, "pattern": pattern, "matches": []})
        return ToolResult.ok(
            f"{len(hits)} match(es):\n" + "\n".join(hits),
            data={"path": real, "pattern": pattern, "matches": hits},
        )

    registry.register(ToolSpec(
        name="search_files", description="Search for files by name pattern (supports * and ?).",
        purpose="Find files", category="files",
        parameters={"pattern": {"type": "string", "required": True}, "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": True},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS}},
        handler=search_files, permission=PermissionLevel.READ_ONLY, timeout=30,
    ))

    async def grep_files(ctx: ToolContext, query: str, path: str = ".", file_pattern: str = "*",
                         max_results: int = 50) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        rx = re.compile(re.escape(file_pattern).replace(r"\*", ".*").replace(r"\?", "."))
        hits: list[str] = []
        for p in Path(real).rglob("*"):
            try:
                if not p.is_file() or not rx.search(p.name):
                    continue
                if p.stat().st_size > MAX_TEXT_BYTES:
                    continue
                with open(p, "rb") as fh:
                    data = fh.read(MAX_TEXT_BYTES)
                if b"\x00" in data[:4096]:
                    continue
                text = data.decode(_detect_encoding(data), errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        hits.append(f"{p}:{lineno}: {line.strip()[:300]}")
                        if len(hits) >= min(max_results, MAX_GREP_RESULTS):
                            break
            except OSError:
                continue
            if len(hits) >= min(max_results, MAX_GREP_RESULTS):
                break
        if not hits:
            return ToolResult.ok(f"No matches for {query!r} under {real}",
                                 data={"query": query, "matches": []})
        return ToolResult.ok(f"{len(hits)} match(es):\n" + "\n".join(hits),
                             data={"query": query, "matches": hits})

    registry.register(ToolSpec(
        name="grep_files", description="Search file contents for a string within a directory tree.",
        purpose="Find text in files", category="files",
        parameters={"query": {"type": "string", "required": True}, "path": {"type": "string", "default": "."},
                    "file_pattern": {"type": "string", "default": "*"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_GREP_RESULTS}},
        handler=grep_files, permission=PermissionLevel.READ_ONLY, timeout=60,
    ))

    # ------------------------------------------------------------- info
    async def file_info(ctx: ToolContext, path: str) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        st = os.stat(real)
        sha = hashlib.sha256()
        with open(real, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        kind = "directory" if os.path.isdir(real) else "file"
        info = {
            "path": real, "kind": kind, "size": st.st_size,
            "created": _fmt_ts(st.st_ctime), "modified": _fmt_ts(st.st_mtime),
            "permissions": oct(st.st_mode & 0o777), "sha256": sha.hexdigest(),
        }
        return ToolResult.ok(
            f"path: {real}\nkind: {kind}\nsize: {st.st_size} bytes\n"
            f"created: {info['created']}\nmodified: {info['modified']}\n"
            f"permissions: {info['permissions']}\nsha256: {info['sha256']}",
            data=info,
        )

    registry.register(ToolSpec(
        name="file_info", description="Get metadata (size, timestamps, permissions, sha256) for a file or directory.",
        purpose="Inspect file metadata", category="files",
        parameters={"path": {"type": "string", "required": True}},
        handler=file_info, permission=PermissionLevel.READ_ONLY, timeout=20,
    ))

    # ------------------------------------------------------------- write
    async def write_file(ctx: ToolContext, path: str, content: str, mode: str = "write") -> ToolResult:
        real = resolve_safe_path(path, allow_write=True)
        parent = os.path.dirname(real) or "."
        os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(real)
        if mode == "append":
            with open(real, "a", encoding="utf-8") as fh:
                fh.write(content)
        elif mode == "write":
            with open(real, "w", encoding="utf-8") as fh:
                fh.write(content)
        else:
            raise ToolError(f"unknown mode: {mode}", kind="invalid")
        size = os.path.getsize(real)
        return ToolResult.ok(
            f"Wrote {len(content)} chars ({size} bytes) to {real} "
            f"({'overwrote existing file' if existed else 'created new file'}); mode={mode}",
            data={"path": real, "size": size, "created": not existed, "mode": mode},
            artifacts=[{"type": "file", "path": real}],
        )

    registry.register(ToolSpec(
        name="write_file", description="Write text content to a file (creates parent dirs; overwrites by default).",
        purpose="Create/edit files", category="files",
        parameters={"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True},
                    "mode": {"type": "string", "enum": ["write", "append"], "default": "write"}},
        handler=write_file, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=20,
        required_permission_notes="Overwriting an existing file is not reversible. Denied outside workspace/home roots.",
    ))

    # ------------------------------------------------------------- dirs
    async def make_directory(ctx: ToolContext, path: str) -> ToolResult:
        real = resolve_safe_path(path, allow_write=True)
        os.makedirs(real, exist_ok=True)
        return ToolResult.ok(f"Directory ready: {real}", data={"path": real})

    registry.register(ToolSpec(
        name="make_directory", description="Create a directory (and parents if needed).",
        purpose="Organize folders", category="files",
        parameters={"path": {"type": "string", "required": True}},
        handler=make_directory, permission=PermissionLevel.SAFE_ACTION, timeout=15,
    ))

    # ------------------------------------------------------------- copy/move/rename/delete
    async def copy_file(ctx: ToolContext, source: str, destination: str) -> ToolResult:
        src = resolve_safe_path(source, must_exist=True)
        dst = resolve_safe_path(destination, allow_write=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
        return ToolResult.ok(f"Copied {src} → {dst}", data={"source": src, "destination": dst},
                             artifacts=[{"type": "file", "path": dst}])

    registry.register(ToolSpec(
        name="copy_file", description="Copy a file or directory to a new location.",
        purpose="Duplicate files", category="files",
        parameters={"source": {"type": "string", "required": True}, "destination": {"type": "string", "required": True}},
        handler=copy_file, permission=PermissionLevel.SAFE_ACTION, timeout=60,
    ))

    async def move_file(ctx: ToolContext, source: str, destination: str) -> ToolResult:
        src = resolve_safe_path(source, must_exist=True)
        dst = resolve_safe_path(destination, allow_write=True)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
        return ToolResult.ok(f"Moved {src} → {dst}", data={"source": src, "destination": dst},
                             artifacts=[{"type": "file", "path": dst}])

    registry.register(ToolSpec(
        name="move_file", description="Move a file or directory to a new location.",
        purpose="Relocate files", category="files",
        parameters={"source": {"type": "string", "required": True}, "destination": {"type": "string", "required": True}},
        handler=move_file, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=60,
        required_permission_notes="Moving can break references; confirmation required by default.",
    ))

    async def rename_file(ctx: ToolContext, path: str, new_name: str) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        parent = os.path.dirname(real)
        dst = resolve_safe_path(os.path.join(parent, new_name), allow_write=True)
        os.rename(real, dst)
        return ToolResult.ok(f"Renamed {real} → {dst}", data={"source": real, "destination": dst})

    registry.register(ToolSpec(
        name="rename_file", description="Rename a file or directory (same folder).",
        purpose="Rename files", category="files",
        parameters={"path": {"type": "string", "required": True}, "new_name": {"type": "string", "required": True}},
        handler=rename_file, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=20,
    ))

    async def delete_file(ctx: ToolContext, path: str) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        if os.path.isdir(real):
            raise ToolError("use delete_directory for folders", kind="invalid")
        size = os.path.getsize(real)
        os.remove(real)
        return ToolResult.ok(f"Deleted {real} ({size} bytes)", data={"path": real, "size": size})

    registry.register(ToolSpec(
        name="delete_file", description="Permanently delete a file (cannot be undone).",
        purpose="Remove files", category="files",
        parameters={"path": {"type": "string", "required": True}},
        handler=delete_file, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=20,
        required_permission_notes="Deletion cannot be undone. Always requires confirmation.",
    ))

    async def delete_directory(ctx: ToolContext, path: str) -> ToolResult:
        real = resolve_safe_path(path, must_exist=True)
        if not os.path.isdir(real):
            raise ToolError("not a directory", kind="invalid")
        count = sum(len(f) for _, _, f in os.walk(real))
        shutil.rmtree(real)
        return ToolResult.ok(f"Deleted directory {real} ({count} files)", data={"path": real, "files": count})

    registry.register(ToolSpec(
        name="delete_directory", description="Recursively delete a directory (cannot be undone).",
        purpose="Remove folders", category="files",
        parameters={"path": {"type": "string", "required": True}},
        handler=delete_directory, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=60,
        required_permission_notes="Recursive deletion cannot be undone. Always requires confirmation.",
    ))

    # ------------------------------------------------------------- archives
    async def compress_archive(ctx: ToolContext, paths: list[str], destination: str,
                               format: str = "zip") -> ToolResult:
        dst = resolve_safe_path(destination, allow_write=True)
        if os.path.exists(dst):
            raise ToolError(f"destination already exists: {destination}", kind="invalid")
        sources = [resolve_safe_path(p, must_exist=True) for p in paths]
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if format == "zip":
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for src in sources:
                    if os.path.isdir(src):
                        for root, _, files in os.walk(src):
                            for f in files:
                                full = os.path.join(root, f)
                                zf.write(full, os.path.relpath(full, os.path.dirname(dst) or "."))
                    else:
                        zf.write(src, os.path.basename(src))
        elif format in ("tar", "tar.gz", "tgz"):
            mode = "w:gz" if format in ("tar.gz", "tgz") else "w"
            with tarfile.open(dst, mode) as tf:
                for src in sources:
                    tf.add(src, arcname=os.path.basename(src))
        else:
            raise ToolError(f"unsupported format: {format}", kind="invalid")
        size = os.path.getsize(dst)
        return ToolResult.ok(f"Created archive {dst} ({size} bytes)", data={"path": dst, "size": size},
                             artifacts=[{"type": "file", "path": dst}])

    registry.register(ToolSpec(
        name="compress_archive", description="Create a zip/tar/tar.gz archive from files or folders.",
        purpose="Archive files", category="files",
        parameters={"paths": {"type": "array", "items": {"type": "string"}, "required": True, "minItems": 1},
                    "destination": {"type": "string", "required": True},
                    "format": {"type": "string", "enum": ["zip", "tar", "tar.gz"], "default": "zip"}},
        handler=compress_archive, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=120,
    ))

    async def extract_archive(ctx: ToolContext, archive: str, destination: str) -> ToolResult:
        src = resolve_safe_path(archive, must_exist=True)
        dst = resolve_safe_path(destination, allow_write=True)
        os.makedirs(dst, exist_ok=True)
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as zf:
                _guard_zip_members(zf)
                zf.extractall(dst)
        elif tarfile.is_tarfile(src):
            with tarfile.open(src) as tf:
                _guard_tar_members(tf)
                tf.extractall(dst)
        else:
            raise ToolError("unsupported archive type (zip/tar/tar.gz supported)", kind="invalid")
        count = sum(len(f) for _, _, f in os.walk(dst))
        return ToolResult.ok(f"Extracted {archive} → {dst} ({count} files)", data={"path": dst, "files": count})

    registry.register(ToolSpec(
        name="extract_archive", description="Extract a zip/tar archive into a directory (traversal-safe).",
        purpose="Unpack archives", category="files",
        parameters={"archive": {"type": "string", "required": True}, "destination": {"type": "string", "required": True}},
        handler=extract_archive, permission=PermissionLevel.CONFIRM_REQUIRED, timeout=120,
    ))


def _guard_zip_members(zf: zipfile.ZipFile) -> None:
    for info in zf.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise ToolError(f"archive contains unsafe path: {name}", kind="permission")
    if len(zf.infolist()) > MAX_ARCHIVE_MEMBERS:
        raise ToolError("archive has too many members", kind="invalid")


def _guard_tar_members(tf: tarfile.TarFile) -> None:
    for member in tf.getmembers():
        if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts:
            raise ToolError(f"archive contains unsafe path: {member.name}", kind="permission")
    if len(tf.getmembers()) > MAX_ARCHIVE_MEMBERS:
        raise ToolError("archive has too many members", kind="invalid")


def _fmt_ts(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
