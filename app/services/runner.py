"""Worker subprocess runner

Popen + reader thread instead of asyncio subprocess: uvicorn on Windows
may run a SelectorEventLoop which can't spawn processes
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import PROJECT_DIR

IS_WINDOWS = os.name == "nt"
MAX_LINE = 64 * 1024


class WorkerProcess:
    def __init__(self, request: dict[str, Any], *, file_limit: int | None = None) -> None:
        self.request = request
        self.file_limit = file_limit
        self.proc: subprocess.Popen | None = None
        self.killed = False

    def _spawn(self) -> subprocess.Popen:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "cwd": str(PROJECT_DIR),
            "env": {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # own process group to kill ffmpeg together with the worker
            kwargs["start_new_session"] = True
        # no preexec_fn: it can deadlock after fork in a threaded process
        return subprocess.Popen([sys.executable, "-m", "app.services.worker"], **kwargs)

    def kill(self) -> None:
        self.killed = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
                )
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass

    async def run(self, on_event: Callable[[dict[str, Any]], Awaitable[None] | None], timeout: float) -> int:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.proc = await asyncio.to_thread(self._spawn)
        proc = self.proc

        def reader() -> None:
            try:
                assert proc.stdin is not None and proc.stdout is not None
                request = {**self.request, "file_limit": self.file_limit}
                proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                proc.stdin.close()
                for raw in proc.stdout:
                    if len(raw) > MAX_LINE:
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        loop.call_soon_threadsafe(queue.put_nowait, event)
            except (OSError, ValueError):
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, name=f"worker-reader-{proc.pid}", daemon=True).start()

        deadline = loop.time() + timeout
        timed_out = False
        finished_normally = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    timed_out = True
                    break
                if event is None:
                    finished_normally = True
                    break
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            if not finished_normally or self.killed:
                # timeout / task cancelled / handler raised
                self.kill()
            elif proc.poll() is None:
                try:
                    await asyncio.to_thread(proc.wait, 10)
                except subprocess.TimeoutExpired:
                    await asyncio.to_thread(self.kill)
        if timed_out:
            raise TimeoutError
        code = await asyncio.to_thread(proc.wait)
        return -9 if self.killed else code
