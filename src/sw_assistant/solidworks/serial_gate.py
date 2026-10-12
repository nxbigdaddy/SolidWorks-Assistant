from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator

from ..domain import ToolError


MUTEX_NAME = "SolidWorksAIAssistant.Automation.SerialGate"
_LOCAL_GATE = threading.Lock()


@contextlib.contextmanager
def solidworks_serial_gate(timeout_seconds: int = 120) -> Iterator[None]:
    """Serialize SolidWorks writes across all instances of this application.

    The project-owned Windows mutex prevents two assistant processes from
    changing the active document or selection set at the same time. Non-Windows
    execution only occurs in unit tests, where a process-local lock is enough.
    """

    timeout = max(1, int(timeout_seconds))
    if os.name != "nt":
        acquired = _LOCAL_GATE.acquire(timeout=timeout)
        if not acquired:
            raise ToolError("等待 SolidWorks 自动化串行锁超时。")
        try:
            yield
        finally:
            _LOCAL_GATE.release()
        return

    try:
        import win32api
        import win32event
    except ImportError as exc:  # pragma: no cover - Windows packaging guard
        raise ToolError("Windows 缺少 pywin32，无法创建 SolidWorks 自动化串行锁。") from exc

    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    acquired = False
    try:
        status = win32event.WaitForSingleObject(handle, timeout * 1000)
        acquired = status in {win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED}
        if not acquired:
            raise ToolError(
                f"等待 SolidWorks 自动化串行锁超过 {timeout} 秒；"
                "可能有另一个助手实例或 SolidWorks 自动化任务仍在运行。"
            )
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(handle)
        win32api.CloseHandle(handle)
