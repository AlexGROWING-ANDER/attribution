"""Platform-specific sandbox, process, and submission-helper policy."""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path


ENGINE_DIR = Path(__file__).resolve().parent
PROTOCOL_DIR = ENGINE_DIR.parent / "protocols"


class PlatformRuntime:
    """Shared defaults for supported native Codex hosts."""

    name = "unsupported"
    helper_name = "io.py"
    protocol_filename = "submission_protocol_posix.md"

    @property
    def protocol_path(self) -> Path:
        return PROTOCOL_DIR / self.protocol_filename

    def sandbox_options(self, workspace, home, runtime_dep=None, read_roots=()) -> dict:
        filesystem = {
            ":root": "deny",
            ":minimal": "read",
            str(Path(workspace)): "write",
            str(Path(home)): "write",
        }
        if runtime_dep is not None:
            filesystem[str(Path(runtime_dep))] = "read"
        for root in read_roots:
            if root:
                filesystem.setdefault(str(Path(root).resolve()), "read")
        return {
            "default_permissions": "d60-workspace",
            "permissions.d60-workspace.extends": ":workspace",
            "permissions.d60-workspace.filesystem": filesystem,
            "permissions.d60-workspace.network.enabled": False,
        }

    def submission_protocol_bytes(self) -> bytes:
        """Return the protocol with the POSIX helper pinned to this interpreter."""
        data = self.protocol_path.read_bytes()
        if self.is_windows():
            return data
        interpreter = shlex.quote(str(Path(sys.executable).resolve()))
        return data.decode("utf-8").replace("python3 io.py", f"{interpreter} io.py").encode("utf-8")

    def approval_args(self) -> list[str]:
        return []

    def approval_options(self) -> dict:
        return {"approval_policy": "on-request", "approvals_reviewer": "auto_review"}

    def default_runtime_dep(self) -> Path | None:
        return None

    def requires_runtime_dep(self) -> bool:
        return False

    def resolve_runtime_dep(self, configured: str | None) -> Path | None:
        value = configured or self.default_runtime_dep()
        if value is None:
            return None
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Set CODEX_RUNTIME_DEP to an installed runtime directory")
        return path

    def popen_kwargs(self) -> dict:
        return {"start_new_session": True}

    def kill_process_tree(self, process, timeout=None) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def is_windows(self) -> bool:
        return False


class WindowsRuntime(PlatformRuntime):
    name = "windows"
    helper_name = "io.ps1"
    protocol_filename = "submission_protocol.md"

    def sandbox_options(self, workspace, home, runtime_dep=None, read_roots=()) -> dict:
        options = super().sandbox_options(workspace, home, runtime_dep, read_roots)
        options["windows.sandbox"] = "mxc"
        return options

    def approval_args(self) -> list[str]:
        return ["--approve-for-me"]

    def approval_options(self) -> dict:
        return {}

    def default_runtime_dep(self) -> Path:
        return Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell"

    def resolve_runtime_dep(self, configured: str | None) -> Path:
        value = configured or self.default_runtime_dep()
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Set CODEX_RUNTIME_DEP to the installed Codex PowerShell runtime directory")
        return path

    def requires_runtime_dep(self) -> bool:
        return True

    def popen_kwargs(self) -> dict:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}

    def kill_process_tree(self, process, timeout=None) -> None:
        kwargs = {
            "capture_output": True,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            **kwargs,
        )

    def is_windows(self) -> bool:
        return True


class MacOSRuntime(PlatformRuntime):
    name = "macos"


class LinuxRuntime(PlatformRuntime):
    name = "linux"


def current_runtime() -> PlatformRuntime:
    if os.name == "nt":
        return WindowsRuntime()
    if sys.platform == "darwin":
        return MacOSRuntime()
    if sys.platform.startswith("linux"):
        return LinuxRuntime()
    return PlatformRuntime()
