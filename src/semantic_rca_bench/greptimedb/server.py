from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx


class EnvironmentError(RuntimeError):
    pass


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def inspect_checkout(
    repo: Path,
    *,
    expected_branch: str | None = None,
    build_profile: Literal["debug", "release"] = "debug",
) -> dict[str, object]:
    if not (repo / "Cargo.toml").is_file():
        raise EnvironmentError(f"not a GreptimeDB checkout: {repo}")
    dirty = _run(repo, "git", "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise EnvironmentError("GreptimeDB has tracked changes; benchmark requires a clean HEAD")
    branch = _run(repo, "git", "branch", "--show-current")
    if expected_branch and branch != expected_branch:
        raise EnvironmentError(f"expected branch {expected_branch}, found {branch}")
    head = _run(repo, "git", "rev-parse", "HEAD")
    binary = repo / "target" / build_profile / "greptime"
    binary_version = None
    if binary.is_file():
        result = subprocess.run(
            [str(binary), "--version"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        binary_version = (result.stdout + result.stderr).strip()
    return {
        "repo": str(repo),
        "branch": branch,
        "head": head,
        "build_profile": build_profile,
        "binary": str(binary),
        "binary_version": binary_version,
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ManagedGreptime:
    binary: Path
    run_dir: Path
    startup_timeout: float = 90.0

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.http_port = _free_port()
        self.grpc_port = _free_port()
        self.mysql_port = _free_port()
        self.postgres_port = _free_port()
        self.process: subprocess.Popen[bytes] | None = None
        self._log = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def start(self) -> None:
        if not self.binary.is_file():
            raise EnvironmentError(f"GreptimeDB binary not found: {self.binary}")
        log_path = self.run_dir / "greptimedb.log"
        self._log = log_path.open("wb")
        command = [
            str(self.binary),
            "standalone",
            "start",
            "--http-addr",
            f"127.0.0.1:{self.http_port}",
            "--grpc-bind-addr",
            f"127.0.0.1:{self.grpc_port}",
            "--mysql-addr",
            f"127.0.0.1:{self.mysql_port}",
            "--postgres-addr",
            f"127.0.0.1:{self.postgres_port}",
            "--data-home",
            str(self.run_dir / "data"),
        ]
        self.process = subprocess.Popen(command, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise EnvironmentError(
                    f"GreptimeDB exited with {self.process.returncode}; see {log_path}"
                )
            try:
                response = httpx.get(f"{self.endpoint}/health", timeout=1.0, trust_env=False)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise EnvironmentError(f"GreptimeDB did not become healthy; see {log_path}")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log:
            self._log.close()

    def __enter__(self) -> ManagedGreptime:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def metadata(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "http_port": self.http_port,
            "grpc_port": self.grpc_port,
            "mysql_port": self.mysql_port,
            "postgres_port": self.postgres_port,
            "run_dir": str(self.run_dir),
        }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
