"""Sandboxed command execution for ctx_store_cmd.

Windows: Job Object with a hard-capped CPU rate, a memory limit, an
active-process limit and kill-on-close.
Linux: bwrap (bubblewrap) or firejail fallback.
Fails closed — if sandboxing unavailable, command is rejected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SandboxResult:
    """Result of a sandboxed command execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    sandboxed: bool
    error: Optional[str] = None
    # True when the CPU rate cap was actually applied (Windows only).
    cpu_limited: Optional[bool] = None


class CommandSandbox:
    """Runs commands in a sandbox with resource limits."""

    # Default limits
    DEFAULT_TIMEOUT_S = 30
    DEFAULT_MAX_MEMORY_MB = 256
    DEFAULT_MAX_CPU_PCT = 50
    DEFAULT_MAX_PROCESSES = 10
    DEFAULT_MAX_FILES_MB = 100

    # JobObjectCpuRateControlInformation (info class 15). pywin32 exposes no
    # binding for it, so the CPU cap goes through kernel32 directly.
    _JOB_CPU_RATE_INFO_CLASS = 15
    _CPU_RATE_ENABLE = 0x1
    _CPU_RATE_HARD_CAP = 0x4

    def __init__(
        self,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
        max_cpu_pct: int = DEFAULT_MAX_CPU_PCT,
        max_processes: int = DEFAULT_MAX_PROCESSES,
        max_files_mb: int = DEFAULT_MAX_FILES_MB,
        allowed_dirs: Optional[list[Path]] = None,
    ):
        self.timeout_s = timeout_s
        self.max_memory_mb = max_memory_mb
        self.max_cpu_pct = max_cpu_pct
        self.max_processes = max_processes
        self.max_files_mb = max_files_mb
        self.allowed_dirs = allowed_dirs or [Path.cwd()]
        self._platform = sys.platform

    def run(self, command: str, cwd: Optional[Path] = None) -> SandboxResult:
        """Execute command in sandbox. Returns SandboxResult."""
        start = time.perf_counter()

        if self._platform.startswith("win"):
            return self._run_windows(command, cwd, start)
        else:
            return self._run_linux(command, cwd, start)

    def _run_windows(
        self, command: str, cwd: Optional[Path], start: float
    ) -> SandboxResult:
        """Windows Job Object sandbox: pywin32 for limits, ctypes for CPU."""
        try:
            import pywintypes
            import win32api
            import win32job
        except ImportError:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                duration_ms=(time.perf_counter() - start) * 1000,
                sandboxed=False,
                error="pywin32 not installed — sandbox unavailable, command rejected",
            )

        job = win32job.CreateJobObject(None, "")
        # Memory, active-process and kill-on-close limits live on the EXTENDED
        # limit class. JOBOBJECT_BASIC_LIMIT_INFORMATION exposes exactly 9 keys
        # and has no ProcessMemoryLimit field, so writing one there raises
        # TypeError and applies no limit at all.
        ext = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        basic = dict(ext["BasicLimitInformation"])
        basic["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        )
        basic["ActiveProcessLimit"] = self.max_processes
        ext["BasicLimitInformation"] = basic
        ext["ProcessMemoryLimit"] = self.max_memory_mb * 1024 * 1024
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, ext
        )
        cpu_limited = self._apply_cpu_rate(job)

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )

            # Assign process to job. Fail closed: a process left outside the
            # job escapes every limit set above.
            try:
                win32job.AssignProcessToJobObject(job, int(proc._handle))
            except pywintypes.error as exc:
                proc.kill()
                proc.communicate()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    duration_ms=(time.perf_counter() - start) * 1000,
                    sandboxed=False,
                    error=f"could not assign process to job object: {exc}",
                )

            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                # Closing the job handle kills the whole tree; proc.kill()
                # alone would leave the shell's children running.
                try:
                    win32api.CloseHandle(job)
                except Exception:
                    pass
                job = None
                proc.communicate()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {self.timeout_s}s",
                    duration_ms=(time.perf_counter() - start) * 1000,
                    sandboxed=True,
                    error="timeout",
                )

            return SandboxResult(
                exit_code=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
                duration_ms=(time.perf_counter() - start) * 1000,
                sandboxed=True,
                cpu_limited=cpu_limited,
            )
        finally:
            if job is not None:
                try:
                    win32api.CloseHandle(job)
                except Exception:
                    pass

    def _apply_cpu_rate(self, job) -> bool:
        """Hard-cap CPU at ``max_cpu_pct`` percent. Returns True when enforced.

        The struct is 8 bytes — ControlFlags plus a 4-byte union — so passing a
        16-byte buffer fails with ERROR_BAD_ENVIRONMENT. Verified on this host.
        """
        if not self.max_cpu_pct:
            return False
        try:
            import ctypes
            import ctypes.wintypes as wt

            class _CpuRateControl(ctypes.Structure):
                _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]

            set_information_job_object = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).SetInformationJobObject
            info = _CpuRateControl()
            info.ControlFlags = self._CPU_RATE_ENABLE | self._CPU_RATE_HARD_CAP
            info.CpuRate = int(self.max_cpu_pct) * 100
            return bool(
                set_information_job_object(
                    ctypes.c_void_p(int(job)),
                    self._JOB_CPU_RATE_INFO_CLASS,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except Exception:
            return False

    def _run_linux(
        self, command: str, cwd: Optional[Path], start: float
    ) -> SandboxResult:
        """Linux sandbox via bwrap or firejail."""
        sandbox_cmd = self._detect_linux_sandbox()
        if not sandbox_cmd:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                duration_ms=(time.perf_counter() - start) * 1000,
                sandboxed=False,
                error="No sandbox (bwrap/firejail) available — command rejected",
            )

        cmd = list(sandbox_cmd) + [
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-net",  # No network access
            "--unshare-ipc",
            "--die-with-parent",
            "--",
            "sh",
            "-c",
            command,
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {self.timeout_s}s",
                duration_ms=(time.perf_counter() - start) * 1000,
                sandboxed=True,
                error="timeout",
            )

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_ms=(time.perf_counter() - start) * 1000,
            sandboxed=True,
        )

    def _detect_linux_sandbox(self) -> Optional[list[str]]:
        """Detect available Linux sandbox tool."""
        if shutil.which("bwrap"):
            return ["bwrap"]
        if shutil.which("firejail"):
            return ["firejail", "--quiet"]
        return None

    def is_available(self) -> bool:
        """Check if sandboxing is available on this platform."""
        if self._platform.startswith("win"):
            try:
                import win32job  # noqa: F401
                return True
            except ImportError:
                return False
        else:
            return self._detect_linux_sandbox() is not None


# Global sandbox instance
_sandbox: Optional[CommandSandbox] = None


def get_sandbox() -> CommandSandbox:
    """Get the global sandbox instance."""
    global _sandbox
    if _sandbox is None:
        _sandbox = CommandSandbox()
    return _sandbox