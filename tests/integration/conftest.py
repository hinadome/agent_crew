"""Integration tests: real sockets, TLS, DNS, subprocesses, Docker and (when you supply credentials) real
providers and chat platforms.

Run them with::

    uv run pytest -m integration -rs

Every test skips with an explicit reason when its prerequisite is missing, so ``-rs`` tells you exactly what was
NOT exercised. Credentials are read from ``KIRO_IT_*`` environment variables (see each module); nothing is read
from your normal AWS/OpenAI/... configuration, so the suite never spends money or posts messages by surprise.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


def need_env(*names: str) -> dict[str, str]:
    """Return the env vars or skip, naming exactly which are missing."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"set {', '.join(missing)} to run this test")
    return {n: os.environ[n] for n in names}


def network_available(host: str = "example.com", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def network() -> None:
    if not network_available():
        pytest.skip("no outbound network access (example.com:443 unreachable)")


@pytest.fixture(scope="session")
def docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        pytest.skip("docker is not installed")
    try:
        subprocess.run([exe, "info"], capture_output=True, timeout=15, check=True)
    except (subprocess.SubprocessError, OSError):
        pytest.skip("docker is installed but the daemon is not reachable")
    return exe
