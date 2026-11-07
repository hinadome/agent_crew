"""``sandbox`` tool group: run_shell, run_python. Pair with ``require_approval`` for human sign-off."""

from __future__ import annotations

import logging
from typing import Any

from strands import tool

from ..core.context import AppContext
from ..core.logging import log
from ..sandbox import SandboxUnavailable, get_sandbox
from . import tool_factories

logger = logging.getLogger("kiro_crew.tools.sandbox")


@tool_factories.register("sandbox")
def _sandbox(ctx: AppContext) -> Any:
    cfg = ctx.config.sandbox
    backend = get_sandbox(cfg)
    workdir = ctx.files_root  # an isolated agent's commands only see its own folder

    async def _run(argv: list[str]) -> str:
        log(logger, logging.DEBUG, "sandbox run", backend=cfg.backend, argv=argv)
        try:
            res = await backend.run(argv, workdir)
        except SandboxUnavailable as e:
            log(logger, logging.INFO, "sandbox unavailable", reason=str(e))
            return f"Sandbox unavailable: {e}"
        log(logger, logging.DEBUG, "sandbox done", code=res.code, timed_out=res.timed_out)
        return res.render(cfg.max_output_chars)

    @tool
    async def run_shell(command: str) -> str:
        """Run a shell command in an isolated sandbox. The working directory is the agent's files folder,
        which is the only persistent, shared location. Network access is off unless configured.

        Args:
            command: Shell command line (executed with `sh -c`).
        """
        return await _run(["sh", "-c", command])

    @tool
    async def run_python(code: str) -> str:
        """Run Python 3 code in an isolated sandbox. The working directory is the agent's files folder.
        Print results to stdout.

        Args:
            code: Python source to execute.
        """
        return await _run(["python3", "-c", code])

    return [run_shell, run_python]
