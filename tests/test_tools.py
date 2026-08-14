"""TIERS 4–5 — real tool execution: files, terminal, processes, system, gui
(headless → honest unavailable), archives, web (unreachable → structured error).
No simulation: everything touches the real OS."""

from __future__ import annotations

import os
import zipfile

import pytest

from phantom_ai.tools.base import ToolError


async def test_write_and_read_file(tool_ctx):
    ctx = tool_ctx()
    path = os.path.join(ctx.workspace_root, "sample.txt")
    w = await ctx.registry_get("write_file").run(ctx, path=path, content="line one\nline two")
    assert w.success and os.path.exists(path)
    r = await ctx.registry_get("read_file").run(ctx, path=path)
    assert "line one" in r.output
    info = await ctx.registry_get("file_info").run(ctx, path=path)
    assert info.data["sha256"]
    assert info.data["size"] == len("line one\nline two")


async def test_list_and_search_and_grep(tool_ctx):
    ctx = tool_ctx()
    base = os.path.join(ctx.workspace_root, "searchtree")
    os.makedirs(base)
    for name, content in [("alpha.py", "def hello():\n    pass\n"),
                          ("beta.md", "# Beta\nhello world here\n"),
                          ("gamma.log", "nothing\n")]:
        with open(os.path.join(base, name), "w") as fh:
            fh.write(content)
    listing = await ctx.registry_get("list_directory").run(ctx, path=base)
    assert "alpha.py" in listing.output
    found = await ctx.registry_get("search_files").run(ctx, pattern="*.py", path=base)
    assert "alpha.py" in found.output
    grep = await ctx.registry_get("grep_files").run(ctx, query="hello", path=base)
    assert "beta.md" in grep.output


async def test_path_traversal_blocked(tool_ctx):
    ctx = tool_ctx()
    with pytest.raises(ToolError) as ei:
        await ctx.registry_get("read_file").run(ctx, path="/etc/passwd")
    assert ei.value.kind == "permission"
    with pytest.raises(ToolError) as ei2:
        await ctx.registry_get("read_file").run(ctx, path="../../../../etc/passwd")
    assert ei2.value.kind == "permission"


async def test_copy_move_rename_delete(tool_ctx):
    ctx = tool_ctx()
    src = os.path.join(ctx.workspace_root, "orig.txt")
    dst = os.path.join(ctx.workspace_root, "copy.txt")
    with open(src, "w") as fh:
        fh.write("data")
    await ctx.registry_get("copy_file").run(ctx, source=src, destination=dst)
    assert os.path.exists(dst)
    moved = os.path.join(ctx.workspace_root, "moved.txt")
    await ctx.registry_get("move_file").run(ctx, source=dst, destination=moved)
    assert not os.path.exists(dst) and os.path.exists(moved)
    await ctx.registry_get("rename_file").run(ctx, path=moved, new_name="renamed.txt")
    renamed = os.path.join(ctx.workspace_root, "renamed.txt")
    assert os.path.exists(renamed)
    await ctx.registry_get("delete_file").run(ctx, path=renamed)
    assert not os.path.exists(renamed)


async def test_archive_roundtrip_and_traversal_guard(tool_ctx):
    ctx = tool_ctx()
    src_dir = os.path.join(ctx.workspace_root, "bundle")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "a.txt"), "w") as fh:
        fh.write("archive me")
    archive = os.path.join(ctx.workspace_root, "bundle.zip")
    out_dir = os.path.join(ctx.workspace_root, "unpacked")
    await ctx.registry_get("compress_archive").run(ctx, paths=[src_dir], destination=archive)
    assert os.path.exists(archive)
    await ctx.registry_get("extract_archive").run(ctx, archive=archive, destination=out_dir)
    assert os.path.exists(os.path.join(out_dir, "bundle", "a.txt"))

    # evil zip: member escaping the target dir
    evil = os.path.join(ctx.workspace_root, "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escape.txt", "pwned")
    with pytest.raises(ToolError) as ei:
        await ctx.registry_get("extract_archive").run(ctx, archive=evil,
                                                      destination=os.path.join(ctx.workspace_root, "x"))
    assert ei.value.kind == "permission"
    assert not os.path.exists(os.path.join(os.path.dirname(ctx.workspace_root), "escape.txt"))


async def test_terminal_capture_and_policy(tool_ctx):
    ctx = tool_ctx()
    res = await ctx.registry_get("run_command").run(
        ctx, command="echo hello && echo world && exit 3", timeout=15)
    assert res.data["exit_code"] == 3
    assert "hello" in res.data["stdout"] and "world" in res.data["stdout"]
    assert res.data["duration_ms"] > 0

    with pytest.raises(ToolError) as ei:
        await ctx.registry_get("run_command").run(ctx, command="rm -rf /", timeout=15)
    assert ei.value.kind == "permission"

    script = await ctx.registry_get("run_script").run(
        ctx, language="python", script="import sys\nprint('py ok')\n", timeout=30)
    assert "py ok" in script.output


async def test_system_tools(tool_ctx):
    ctx = tool_ctx()
    info = await ctx.registry_get("system_info").run(ctx)
    assert info.data["hostname"]
    res = await ctx.registry_get("system_resources").run(ctx)
    assert "cpu_percent" in res.data
    procs = await ctx.registry_get("list_processes").run(ctx, limit=10)
    assert isinstance(procs.data["processes"], list)


async def test_screenshot_headless_is_honest(tool_ctx):
    ctx = tool_ctx()
    try:
        res = await ctx.registry_get("take_screenshot").run(ctx)
    except ToolError as ei:
        assert ei.kind == "unavailable"
    else:
        # A display exists — screenshot must be a real file
        assert res.artifacts and os.path.exists(res.artifacts[0]["path"])


async def test_web_tools_unreachable_give_structured_error(tool_ctx):
    ctx = tool_ctx()
    with pytest.raises(ToolError) as ei:
        await ctx.registry_get("web_search").run(ctx, query="hello world")
    assert ei.value.kind in ("network", "timeout")


async def test_http_request_ssrf_guard(tool_ctx):
    ctx = tool_ctx()
    with pytest.raises(ToolError) as ei:
        await ctx.registry_get("http_request").run(ctx, url="http://127.0.0.1:9/admin")
    assert ei.value.kind == "permission"


async def test_memory_tools_via_registry(tool_ctx):
    ctx = tool_ctx()
    await ctx.registry_get("remember").run(ctx, content="Deployments happen on Fridays",
                                           kind="decision", importance=0.8)
    found = await ctx.registry_get("search_memories").run(ctx, query="Fridays")
    assert "Fridays" in found.output
    listed = await ctx.registry_get("list_memories").run(ctx)
    assert "Deployments" in listed.output
