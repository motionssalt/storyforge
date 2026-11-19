"""
Live step logger and weighted progress reporter for StoryForge GitHub Actions pipelines.
Reports batched logs and FFmpeg progress percentages to the Cloudflare Worker internal endpoint.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class TaskReporter:
    def __init__(
        self,
        worker_url: str | None,
        secret: str | None,
        task_id: str,
        flush_interval_sec: float = 2.0,
        max_buffer: int = 6,
    ) -> None:
        self.worker_url = worker_url.rstrip("/") if worker_url else ""
        self.secret = secret or ""
        self.task_id = task_id
        self.flush_interval_sec = flush_interval_sec
        self.max_buffer = max_buffer
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        self._progress_pct = 0.0
        self._current_stage = "queued"

    def set_stage(
        self,
        stage: str,
        progress_pct: float | None = None,
        message: str | None = None,
        level: str = "info",
        flush: bool = True,
    ) -> None:
        with self._lock:
            self._current_stage = stage
            if progress_pct is not None:
                self._progress_pct = max(0.0, min(100.0, float(progress_pct)))
            if message:
                self._buffer.append(
                    {
                        "ts": utc_now_iso(),
                        "level": str(level or "info"),
                        "stage": stage,
                        "message": message,
                        "progress_pct": round(self._progress_pct, 1),
                    }
                )
        if flush:
            self.flush()

    def log(
        self,
        stage: str,
        message: str,
        level: str | float | int = "info",
        progress_pct: float | None = None,
        flush: bool = False,
    ) -> None:
        if isinstance(level, (int, float)):
            progress_pct = float(level)
            level = "info"
        level_str = str(level or "info")
        print(f"[{stage}] {level_str.upper()}: {message}", flush=True)
        should_flush = flush
        with self._lock:
            self._current_stage = stage
            if progress_pct is not None:
                self._progress_pct = max(0.0, min(100.0, float(progress_pct)))
            self._buffer.append(
                {
                    "ts": utc_now_iso(),
                    "level": level_str,
                    "stage": stage,
                    "message": message,
                    "progress_pct": round(self._progress_pct, 1),
                }
            )
            if (
                len(self._buffer) >= self.max_buffer
                or (time.monotonic() - self._last_flush) >= self.flush_interval_sec
            ):
                should_flush = True
        if should_flush:
            self.flush()

    def update_progress(self, stage: str, progress_pct: float, message: str | None = None) -> None:
        should_flush = False
        with self._lock:
            self._current_stage = stage
            self._progress_pct = max(0.0, min(100.0, float(progress_pct)))
            if message:
                self._buffer.append(
                    {
                        "ts": utc_now_iso(),
                        "level": "info",
                        "stage": stage,
                        "message": message,
                        "progress_pct": round(self._progress_pct, 1),
                    }
                )
            if (time.monotonic() - self._last_flush) >= self.flush_interval_sec:
                should_flush = True
        if should_flush:
            self.flush()

    def flush(self, status: str | None = None, error: str | None = None) -> None:
        if not self.worker_url or not self.secret:
            with self._lock:
                self._buffer.clear()
            return
        with self._lock:
            logs = list(self._buffer)
            self._buffer.clear()
            self._last_flush = time.monotonic()
            pct = round(self._progress_pct, 1)
            stage = self._current_stage

        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "progress_pct": pct,
            "current_stage": stage,
            "logs": logs,
        }
        if status is not None:
            payload["status"] = status
        if error is not None:
            payload["error"] = error

        try:
            req = urllib.request.Request(
                f"{self.worker_url}/api/internal/task-log",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "X-Internal-Secret": self.secret,
                    "Content-Type": "application/json",
                    "User-Agent": "StoryForge-Runner",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except Exception as exc:
            print(f"[progress] warn: flush failed: {exc}", file=sys.stderr)


def run_ffmpeg_with_progress(
    cmd: list[str],
    duration_sec: float,
    on_progress: Callable[[float], None] | None = None,
    stdin_feeder: Callable[[Any], None] | None = None,
) -> None:
    """
    Execute an ffmpeg command with `-progress pipe:1 -nostats` and stream real-time
    fraction progress (0.0..1.0) without deadlocking stdout/stderr/stdin.
    """
    # Insert -progress pipe:1 -nostats right after "ffmpeg"
    full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess_popen(full_cmd, has_stdin=stdin_feeder is not None)

    stderr_chunks: list[str] = []
    last_emit = [0.0]
    last_pct = [-1.0]

    def read_stdout() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    val = int(line.split("=", 1)[1])
                    # Note: ffmpeg -progress out_time_ms is actually microseconds in many builds, out_time_us is always microseconds
                    out_sec = val / 1_000_000.0
                    frac = max(0.0, min(0.99, out_sec / max(0.1, float(duration_sec))))
                    now = time.monotonic()
                    if on_progress and (now - last_emit[0] >= 1.5 or frac - last_pct[0] >= 0.05):
                        last_emit[0] = now
                        last_pct[0] = frac
                        on_progress(frac)
                except ValueError:
                    pass
            elif line == "progress=end":
                if on_progress:
                    on_progress(1.0)

    def read_stderr() -> None:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            txt = raw_line.decode("utf-8", errors="replace")
            stderr_chunks.append(txt)
            if len(stderr_chunks) > 80:
                del stderr_chunks[:20]

    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    if stdin_feeder is not None and proc.stdin is not None:
        try:
            stdin_feeder(proc.stdin)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    ret = proc.wait()
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    if ret != 0:
        err_tail = "".join(stderr_chunks)[-1500:].strip()
        raise RuntimeError(f"FFmpeg exited with code {ret}: {err_tail}")


def subprocess_popen(cmd: list[str], has_stdin: bool):
    import subprocess

    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if has_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
