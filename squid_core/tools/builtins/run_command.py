"""Own cancellable ``run_command`` execution and failure diagnostics."""

import errno
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from squid_core.platform_context import PlatformContext, detect_platform_context

from ._truncation import truncate_head_tail


TOOL_NAME = "run_command"
DEFAULT_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 0.05
PROCESS_TREE_GRACE_SECONDS = 0.2
PROCESS_DRAIN_TIMEOUT_SECONDS = 0.5
PROCESS_WAIT_TIMEOUT_SECONDS = 0.2
TASKKILL_TIMEOUT_SECONDS = 0.5

_POWERSHELL_COMMAND_NOT_FOUND = re.compile(
    r"The term '.+?' is not recognized as the name of a cmdlet, function, "
    r"script file, or operable program",
    re.IGNORECASE | re.DOTALL,
)
_POWERSHELL_INVALID_SEPARATOR = re.compile(
    r"The token '(?:&&|\|\|)' is not a valid statement separator",
    re.IGNORECASE,
)


def tool_definition() -> dict:
    """Return the command Tool definition shown to the model."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Run one shell command from the repository root and return its "
                "combined output and exit status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "verification": {
                        "type": "boolean",
                        "description": (
                            "REQUIRED. True when this command checks whether the current "
                            "implementation is correct; false otherwise."
                        ),
                    },
                },
                "required": ["command", "verification"],
            },
        },
    }


def execute(
    repo_root: Path,
    command: str,
    verification: bool,
    platform_context: PlatformContext | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """Run one owned process tree and return its normalized result."""
    platform_context = platform_context or detect_platform_context()
    should_cancel = should_cancel or (lambda: False)
    subprocess_command: str | list[str] = command
    use_shell = True
    popen_options = {}
    if platform_context.is_windows:
        subprocess_command = [
            platform_context.shell_executable,
            "-NoProfile",
            "-Command",
            command,
        ]
        use_shell = False
        popen_options["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        # A new session gives the Tool an exact process group to terminate.
        popen_options["start_new_session"] = True

    try:
        process = subprocess.Popen(
            subprocess_command,
            shell=use_shell,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **popen_options,
        )
    except OSError as exc:
        return _launch_failure(command, verification, platform_context, exc)

    # A Job Object closes the Windows race between terminating PowerShell and
    # descendants retaining its output pipe. Assignment can be unavailable in
    # constrained hosts, so bounded taskkill remains the fallback there.
    windows_job = (
        _create_windows_job(process) if platform_context.is_windows else None
    )
    cleanup_completed = False
    try:
        deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
        cancelled = False
        timed_out = False
        output_bytes = b""
        while True:
            if should_cancel():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                output_bytes, _ = process.communicate(
                    timeout=min(POLL_INTERVAL_SECONDS, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue

        if cancelled or timed_out:
            cleanup_completed = True
            output_bytes = _cleanup_owned_process(
                process,
                platform_context.is_windows,
                windows_job,
            )
    except BaseException:
        # KeyboardInterrupt is intentionally re-raised, but never before the
        # Tool has relinquished every bounded resource it owns.
        cleanup_completed = True
        _cleanup_owned_process(
            process,
            platform_context.is_windows,
            windows_job,
        )
        raise
    finally:
        if windows_job is not None:
            # Normal completion preserves the historical ability of an
            # explicitly detached process to outlive its launching shell.
            windows_job.close(kill_owned=cleanup_completed)

    output = (output_bytes or b"").decode("utf-8", errors="replace")
    # Match subprocess text mode's universal-newline behavior on every platform.
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    output, output_metadata = truncate_head_tail(output)
    if cancelled or timed_out:
        response = _base_response(
            command, verification, output, output_metadata, returncode=-1
        )
        response.update(
            {
                "cancelled": cancelled,
                "timed_out": timed_out,
                "failure_class": "cancelled" if cancelled else "timeout",
                "shell": platform_context.shell_name,
                "platform": platform_context.os_name,
            }
        )
        return response

    response = _base_response(
        command,
        verification,
        output,
        output_metadata,
        returncode=process.returncode,
    )
    response["ok"] = process.returncode == 0
    if process.returncode != 0:
        response.update(
            {
                "failure_class": _classify_failure(
                    platform_context, process.returncode, output
                ),
                "shell": platform_context.shell_name,
                "platform": platform_context.os_name,
            }
        )
    return response


def _cleanup_owned_process(
    process: subprocess.Popen,
    is_windows: bool,
    windows_job: "_WindowsJob | None" = None,
) -> bytes:
    """Terminate one owned tree and drain only within explicit deadlines."""
    _terminate_process_tree(process, is_windows, windows_job)
    try:
        output, _ = process.communicate(timeout=PROCESS_DRAIN_TIMEOUT_SECONDS)
        return output or b""
    except subprocess.TimeoutExpired as error:
        # A missed or deliberately detached descendant can retain the inherited
        # pipe after its shell is dead. Do not turn pipe EOF into an unbounded
        # cancellation wait; close our endpoints and retain any available bytes.
        output = error.output if isinstance(error.output, bytes) else b""
        _close_process_pipes(process)
        _bounded_process_wait(process)
        return output
    except (OSError, ValueError):
        _close_process_pipes(process)
        _bounded_process_wait(process)
        return b""


def _terminate_process_tree(
    process: subprocess.Popen,
    is_windows: bool,
    windows_job: "_WindowsJob | None" = None,
) -> None:
    """Terminate descendants using bounded platform process-tree primitives."""
    if is_windows:
        # Sweep the visible tree before terminating the Job. This covers the
        # narrow interval between CreateProcess and assignment to the Job.
        _run_taskkill_bounded(process.pid)
        if windows_job is not None:
            windows_job.terminate()
        if process.poll() is None:
            _kill_direct_process(process)
        return

    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        # Cleanup remains best-effort if the host unexpectedly revokes process
        # ownership; the direct process and pipes are still bounded below.
        return

    deadline = time.monotonic() + PROCESS_TREE_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(min(POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0)))

    # Inspect the group itself, not only the original leader. A shell may exit
    # promptly while a descendant ignores SIGTERM and still owns the pipe.
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass


def _process_group_exists(process_group: int) -> bool:
    """Return whether POSIX still exposes any member of a process group."""
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_taskkill_bounded(pid: int) -> None:
    """Run the Windows tree fallback without inheriting subprocess.run waits."""
    try:
        taskkill = subprocess.Popen(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return
    try:
        taskkill.wait(timeout=TASKKILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_direct_process(taskkill)
        _wait_bounded(taskkill, PROCESS_WAIT_TIMEOUT_SECONDS)


def _kill_direct_process(process: subprocess.Popen) -> None:
    """Best-effort kill without allowing cleanup errors to mask cancellation."""
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass


def _bounded_process_wait(process: subprocess.Popen) -> None:
    """Reap the direct child when possible, never waiting without a deadline."""
    if process.poll() is None:
        _kill_direct_process(process)
    _wait_bounded(process, PROCESS_WAIT_TIMEOUT_SECONDS)


def _wait_bounded(process: subprocess.Popen, timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _close_process_pipes(process: subprocess.Popen) -> None:
    """Release parent pipe handles when descendants prevent EOF."""
    for name in ("stdin", "stdout", "stderr"):
        pipe = getattr(process, name, None)
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


@dataclass
class _WindowsJob:
    """Small ctypes-backed owner for a Windows command process tree."""

    handle: int
    kernel32: object

    def terminate(self) -> None:
        try:
            self.kernel32.TerminateJobObject(self.handle, 1)
        except (AttributeError, OSError):
            pass

    def close(self, *, kill_owned: bool) -> None:
        if not self.handle:
            return
        if not kill_owned:
            try:
                _set_job_kill_on_close(self.kernel32, self.handle, enabled=False)
            except OSError:
                # If disarming fails, retain the safety invariant: closing the
                # Job terminates its owned tree rather than leaking it.
                pass
        try:
            self.kernel32.CloseHandle(self.handle)
        finally:
            self.handle = 0


def _create_windows_job(process: subprocess.Popen) -> _WindowsJob | None:
    """Assign a Windows child to a kill-on-close Job when the host permits it."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            return None
        job = _WindowsJob(handle=job_handle, kernel32=kernel32)
        try:
            _set_job_kill_on_close(kernel32, job_handle, enabled=True)
            process_handle = int(getattr(process, "_handle"))
            if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except (AttributeError, OSError, TypeError, ValueError):
            job.close(kill_owned=False)
            return None
        return job
    except (ImportError, AttributeError, OSError):
        return None


def _set_job_kill_on_close(kernel32, job_handle: int, *, enabled: bool) -> None:
    """Configure the one Job limit Code Squid relies on."""
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000 if enabled else 0
    if not kernel32.SetInformationJobObject(
        job_handle,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")


def _base_response(
    command: str,
    verification: bool,
    output: str,
    output_metadata: dict,
    *,
    returncode: int,
) -> dict:
    return {
        "ok": False,
        "tool": TOOL_NAME,
        "command": command,
        "verification": verification,
        "output": output,
        "output_truncated": output_metadata["truncated"],
        "output_original_chars": output_metadata["original_chars"],
        "output_returned_chars": output_metadata["returned_chars"],
        "returncode": returncode,
    }


def _launch_failure(
    command: str,
    verification: bool,
    platform_context: PlatformContext,
    exc: OSError,
) -> dict:
    output, metadata = truncate_head_tail(str(exc))
    if exc.errno in {errno.EACCES, errno.EPERM}:
        failure_class = "permission_denied"
    elif exc.errno == errno.ENOENT:
        failure_class = "path_not_found"
    else:
        failure_class = "unknown_failure"
    response = _base_response(command, verification, output, metadata, returncode=-1)
    response.update(
        {
            "failure_class": failure_class,
            "shell": platform_context.shell_name,
            "platform": platform_context.os_name,
        }
    )
    return response


def _classify_failure(
    platform_context: PlatformContext,
    returncode: int,
    output: str,
) -> str:
    """Classify only strong shell/process signals from a completed command."""
    if platform_context.is_windows:
        if (
            _POWERSHELL_INVALID_SEPARATOR.search(output)
            and re.search(r"At line:\d+ char:\d+", output)
        ) or (
            re.search(r"At line:\d+ char:\d+", output)
            and re.search(r"CategoryInfo\s*:\s*ParserError", output)
        ) or (
            re.search(r"^ParserError:\s*$", output, re.MULTILINE)
            and re.search(r"^Line \|\s*$", output, re.MULTILINE)
        ):
            return "shell_syntax_error"
        if _POWERSHELL_COMMAND_NOT_FOUND.search(output) and (
            "CommandNotFoundException" in output
            or re.search(r"CategoryInfo\s*:\s*ObjectNotFound", output)
        ):
            return "command_not_found"
        if re.search(
            r"Cannot find path '.+?' because it does not exist",
            output,
            re.IGNORECASE | re.DOTALL,
        ) and (
            "ItemNotFoundException" in output
            or re.search(r"CategoryInfo\s*:\s*ObjectNotFound", output)
        ):
            return "path_not_found"
        if "UnauthorizedAccessException" in output or re.search(
            r"CategoryInfo\s*:\s*PermissionDenied", output
        ):
            return "permission_denied"
        return "process_failed"

    shell_name = re.escape(platform_context.shell_name)
    shell_prefix = rf"^(?:.*/)?{shell_name}:\s*"
    if returncode == 127 and (
        re.search(
            shell_prefix
            + r"(?:(?:line )?\d+:\s*)?(?:[^:\r\n]+:\s*)?"
            + r"(?:command )?not found\s*$",
            output,
            re.IGNORECASE | re.MULTILINE,
        )
        or re.search(
            shell_prefix + r"\d+:\s*command not found:\s*[^\r\n]+$",
            output,
            re.IGNORECASE | re.MULTILINE,
        )
    ):
        return "command_not_found"
    if returncode == 126 and re.search(
        shell_prefix + r".*Permission denied\s*$",
        output,
        re.IGNORECASE | re.MULTILINE,
    ):
        return "permission_denied"
    if returncode == 2 and re.search(
        rf"^(?:.*/)?{shell_name}:.*syntax error",
        output,
        re.IGNORECASE | re.MULTILINE,
    ):
        return "shell_syntax_error"
    if re.search(
        rf"^(?:.*/)?{shell_name}:.*cd:.*No such file or directory",
        output,
        re.IGNORECASE | re.MULTILINE,
    ):
        return "path_not_found"
    return "process_failed"
