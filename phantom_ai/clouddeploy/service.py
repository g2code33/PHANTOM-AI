"""Cloud redeploy from the app — no terminal needed.

Runs `bash cloud/deploy.sh` (non-interactive mode) as a background task so the
user can redeploy the Portable Worker straight from Settings and watch the
output. Secrets are passed via env (the deploy script reads them), never
logged. Requires the repo + bash + wrangler on the machine — the PC app already
needs all of those for normal operation.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from ..config import ROOT_DIR


class CloudDeploy:
    def __init__(self, secrets: Any, tasks: Any, events: Any,
                 audit: Any, repo_root: str | None = None) -> None:
        self.secrets = secrets
        self.tasks = tasks
        self.events = events
        self.audit = audit
        self.repo_root = repo_root or str(ROOT_DIR)

    async def deploy(self, nvidia_key: str = "", deepgram_key: str = "",
                     cloud_token: str = "") -> dict[str, Any]:
        """Launch cloud/deploy.sh as a tracked task. Keys/token are passed via
        env to the script (deploy.sh reads them from env and skips prompts)."""
        script = Path(self.repo_root) / "cloud" / "deploy.sh"
        if not script.exists():
            raise RuntimeError("cloud/deploy.sh not found (is the repo cloned?)")
        env = dict(os.environ)
        if nvidia_key:
            env["NVIDIA_API_KEY"] = nvidia_key
        if deepgram_key:
            env["DEEPGRAM_API_KEY"] = deepgram_key
        if cloud_token:
            env["PHANTOM_CLOUD_TOKEN"] = cloud_token
        env["NONINTERACTIVE"] = "1"

        async def factory(task_id: str) -> dict[str, Any]:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(script),
                cwd=self.repo_root, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            lines: list[str] = []
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                lines.append(text)
                await self.tasks.store.append_log(task_id, text)
                await self.events.publish("cloud.deploy_log", {"line": text})
            await proc.wait()
            return {"exit_code": proc.returncode, "lines": lines[-50:]}

        task = await self.tasks.launch("user", "Redeploy Portable Phantom",
                                       "cloud_deploy", factory)
        await self.audit.record("user", "cloud.deploy_started", {})
        return task

    async def last_logs(self) -> list[str]:
        tasks = await self.tasks.store.list(status="running")
        tasks += await self.tasks.store.list(status="completed")
        for t in tasks:
            if t["kind"] == "cloud_deploy":
                return t.get("logs") or []
        return []
