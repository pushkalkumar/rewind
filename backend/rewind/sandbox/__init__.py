"""Sandbox interface. One contract, three backends (local tree, docker, daytona) with identical behavior.

A sandbox is created from a *tree*: a directory holding the repo at its broken state (no .git).
On create, the backend copies the tree in, `git init`s it and commits a baseline so `diff()` is
exactly what the agent changed. `run()` executes a command with cwd at the repo root using a Python
that has the repo's dependencies installed.
"""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    seconds: float = 0.0


class Sandbox(ABC):
    name: str = "abstract"

    @abstractmethod
    def list_files(self, path: str = ".", max_entries: int = 400) -> list[str]:
        """Relative paths under `path` (recursive), skipping .git/venv/__pycache__. Dirs end with '/'."""

    @abstractmethod
    def read_file(self, path: str) -> str: ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    def run(self, cmd: list[str], timeout: int = 120, env: Optional[dict[str, str]] = None) -> RunResult:
        """Run a command at the repo root. `cmd[0] == "python"` must resolve to the sandbox's python."""

    @abstractmethod
    def diff(self) -> str:
        """Unified diff of everything the agent changed versus the baseline commit (git add -A; git diff --cached)."""

    @abstractmethod
    def changed_files(self) -> list[str]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SandboxSpec:
    """What a backend needs to build a sandbox."""

    def __init__(self, tree: Path, venv_python: Optional[Path] = None, install_cmds: Optional[list[list[str]]] = None):
        self.tree = Path(tree)  # repo at broken state, no .git inside
        self.venv_python = venv_python  # local backend: reuse a prepared venv (fast)
        self.install_cmds = install_cmds or [["python", "-m", "pip", "install", "-q", "-e", ".", "pytest"]]


def detect_backend(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if os.environ.get("DAYTONA_API_KEY"):
        return "daytona"
    if shutil.which("docker"):
        return "docker"
    return "local"


def create_sandbox(spec: SandboxSpec, backend: str = "auto") -> Sandbox:
    backend = detect_backend(backend)
    if backend == "local":
        from .local import LocalSandbox
        return LocalSandbox.create(spec)
    if backend == "docker":
        from .docker import DockerSandbox
        return DockerSandbox.create(spec)
    if backend == "daytona":
        from .daytona import DaytonaSandbox
        return DaytonaSandbox.create(spec)
    raise ValueError(f"unknown sandbox backend {backend!r}")
