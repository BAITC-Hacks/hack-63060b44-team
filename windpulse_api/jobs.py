from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .catalog import empty_stages, read_json

JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
TERMINAL = {"completed", "failed", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def process_alive(pid):
    if not isinstance(pid, int) or pid < 1:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            result = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(result))) and result.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class BusyError(RuntimeError):
    pass


class JobManager:
    """One cache-only worker across API processes, backed by an atomic lock file."""

    def __init__(self, root: Path, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.directory = root / "artifacts/web_jobs"
        self.lock_path = self.directory / ".active.lock"
        self._mutex = threading.Lock()

    def recover(self):
        if not self.directory.exists():
            return
        lock = read_json(self.lock_path, {})
        for path in self.directory.glob("*/job.json"):
            job = read_json(path)
            if not job or job.get("status") in TERMINAL:
                continue
            # A surviving detached worker owns its job across an API restart.
            if process_alive(job.get("worker_pid")) or (lock.get("job_id") == job.get("job_id") and process_alive(lock.get("owner_pid"))):
                continue
            job.update(status="interrupted", finished_at=now(),
                       error={"message": "Предыдущий расчёт был прерван.", "action": "Запустите расчёт повторно.",
                              "detail": "Процесс задания больше не выполняется."})
            for stage in job["stages"]:
                if stage["status"] == "running":
                    stage.update(status="error", evidence="Процесс прерван; длительность неизвестна.")
            write_json(path, job)
        if self.lock_path.exists() and lock:
            job = read_json(self.directory / str(lock.get("job_id", "")) / "job.json")
            if job and job.get("status") in TERMINAL:
                self.lock_path.unlink(missing_ok=True)

    def get(self, job_id):
        if not JOB_ID.fullmatch(job_id):
            raise KeyError(job_id)
        job = read_json(self.directory / job_id / "job.json")
        if job is None:
            raise KeyError(job_id)
        return {key: value for key, value in job.items() if key not in ("worker_pid", "config_path")}

    def active(self):
        lock = read_json(self.lock_path, {})
        job_id = lock.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            return None
        try:
            job = self.get(job_id)
            return job if job["status"] not in TERMINAL else None
        except KeyError:
            return None

    def create(self, issue_time, horizon, model):
        with self._mutex:
            self.recover()
            self.directory.mkdir(parents=True, exist_ok=True)
            job_id = uuid.uuid4().hex
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc:
                raise BusyError("Другой расчёт уже выполняется. Дождитесь его завершения.") from exc
            try:
                os.write(descriptor, json.dumps({"job_id": job_id, "owner_pid": os.getpid()}).encode())
            finally:
                os.close(descriptor)
            job = {"job_id": job_id, "status": "queued", "issue_time": issue_time, "horizon": horizon,
                   "model": model, "created_at": now(), "started_at": None, "finished_at": None,
                   "run_id": None, "error": None, "stages": empty_stages(), "events": [],
                   "worker_pid": None, "config_path": str(self.config_path)}
            path = self.directory / job_id
            write_json(path / "job.json", job)
            try:
                output = (path / "worker.log").open("ab")
                try:
                    command = [sys.executable, "-m", "windpulse_api.worker", "--root", str(self.root), "--job", job_id]
                    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, **options)
                finally:
                    output.close()
                # The child owns job.json after launch. Keep its PID in the lock so
                # there is no parent/child read-modify-write race on job stages.
                write_json(self.lock_path, {"job_id": job_id, "owner_pid": process.pid})
                threading.Thread(target=self._watch, args=(process, job_id), daemon=True).start()
            except Exception as exc:
                job.update(status="failed", finished_at=now(), error={"message": "Не удалось запустить процесс расчёта.",
                           "action": "Проверьте установку зависимостей и повторите запуск.", "detail": str(exc)})
                write_json(path / "job.json", job)
                self.lock_path.unlink(missing_ok=True)
            return self.get(job_id)

    def _watch(self, process, job_id):
        code = process.wait()
        with self._mutex:
            path = self.directory / job_id / "job.json"
            job = read_json(path)
            if job and job.get("status") not in TERMINAL:
                job.update(status="failed", finished_at=now(),
                           error={"message": "Процесс расчёта завершился без результата.",
                                  "action": "Проверьте локальные зависимости и журнал worker.log, затем повторите расчёт.",
                                  "detail": f"Код завершения: {code}"})
                for stage in job["stages"]:
                    if stage["status"] == "running":
                        stage.update(status="error", evidence="Процесс завершился до подтверждения этапа.")
                write_json(path, job)
            lock = read_json(self.lock_path, {})
            if lock.get("job_id") == job_id:
                self.lock_path.unlink(missing_ok=True)
