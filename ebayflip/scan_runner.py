from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ebayflip.dashboard_data import scan_age_seconds


ROOT_DIR = Path(__file__).resolve().parent.parent
SCANNER_PATH = ROOT_DIR / "scanner" / "run_scan.py"
CACHE_DIR = ROOT_DIR / ".cache"
STATUS_PATH = CACHE_DIR / "scan_status.json"
LOCK_PATH = CACHE_DIR / "scan.lock"
LOG_PATH = CACHE_DIR / "scan_last.log"

_THREAD_LOCK = threading.Lock()
_SCAN_THREAD: Optional[threading.Thread] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(text: str, limit: int = 4000) -> str:
    if not text:
        return ""
    return text[-limit:]


@dataclass(frozen=True, slots=True)
class ScanStatus:
    status: str  # idle|running|ok|error
    started_at: str | None = None
    ended_at: str | None = None
    returncode: int | None = None
    message: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None


def _write_status(status: ScanStatus) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATUS_PATH.write_text(json.dumps(asdict(status), indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_status() -> ScanStatus:
    try:
        raw = STATUS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return ScanStatus(
                status=str(data.get("status") or "idle"),
                started_at=data.get("started_at"),
                ended_at=data.get("ended_at"),
                returncode=data.get("returncode"),
                message=data.get("message"),
                stdout_tail=data.get("stdout_tail"),
                stderr_tail=data.get("stderr_tail"),
            )
    except OSError:
        pass
    except Exception:
        pass
    return ScanStatus(status="idle")


def scan_run_status() -> dict[str, Any]:
    """Status payload for UI rendering."""
    s = _read_status()
    return {
        "status": s.status,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "returncode": s.returncode,
        "message": s.message,
        "stdout_tail": s.stdout_tail,
        "stderr_tail": s.stderr_tail,
    }


def _lock_is_stale(*, ttl_seconds: int) -> bool:
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
        return age > ttl_seconds
    except OSError:
        return True


def _acquire_lock(*, ttl_seconds: int) -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists() and not _lock_is_stale(ttl_seconds=ttl_seconds):
        return False
    if LOCK_PATH.exists():
        try:
            LOCK_PATH.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\nstarted_at={_now_iso()}\n")
        return True
    except OSError:
        return False


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def _scan_thread_body(*, timeout_seconds: int) -> None:
    started_at = _now_iso()
    _write_status(ScanStatus(status="running", started_at=started_at, message="Scan started"))

    if not SCANNER_PATH.exists():
        ended_at = _now_iso()
        _write_status(
            ScanStatus(
                status="error",
                started_at=started_at,
                ended_at=ended_at,
                returncode=127,
                message="scanner/run_scan.py not found",
            )
        )
        _release_lock()
        return

    cmd = [sys.executable, str(SCANNER_PATH), "--max-cycles", "1"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout_tail = _tail(proc.stdout or "")
        stderr_tail = _tail(proc.stderr or "")
        ended_at = _now_iso()
        ok = proc.returncode == 0
        _write_status(
            ScanStatus(
                status="ok" if ok else "error",
                started_at=started_at,
                ended_at=ended_at,
                returncode=proc.returncode,
                message="Scan completed" if ok else "Scan failed",
                stdout_tail=stdout_tail or None,
                stderr_tail=stderr_tail or None,
            )
        )
        try:
            LOG_PATH.write_text(
                f"$ {' '.join(cmd)}\n\n[stdout]\n{proc.stdout}\n\n[stderr]\n{proc.stderr}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    except subprocess.TimeoutExpired:
        ended_at = _now_iso()
        _write_status(
            ScanStatus(
                status="error",
                started_at=started_at,
                ended_at=ended_at,
                returncode=124,
                message=f"Scan timed out after {timeout_seconds}s",
            )
        )
    except Exception as exc:
        ended_at = _now_iso()
        _write_status(
            ScanStatus(
                status="error",
                started_at=started_at,
                ended_at=ended_at,
                returncode=1,
                message=f"Scan crashed: {exc}",
            )
        )
    finally:
        _release_lock()
        with _THREAD_LOCK:
            global _SCAN_THREAD
            _SCAN_THREAD = None


def trigger_background_scan(*, force: bool = False, timeout_seconds: int = 600) -> bool:
    """Start a background one-shot scan. Returns True if started."""
    min_interval = max(10, int(os.getenv("AUTO_SCAN_MIN_INTERVAL_SECONDS", "300")))
    lock_ttl = max(60, int(os.getenv("AUTO_SCAN_LOCK_TTL_SECONDS", "1200")))

    # Rate limit based on last end time.
    if not force:
        last = _read_status()
        if last.ended_at:
            try:
                ended = datetime.fromisoformat(last.ended_at)
                age = (datetime.now(timezone.utc) - ended).total_seconds()
                if age < min_interval:
                    return False
            except Exception:
                pass

    with _THREAD_LOCK:
        global _SCAN_THREAD
        if _SCAN_THREAD is not None and _SCAN_THREAD.is_alive():
            return False
        if not _acquire_lock(ttl_seconds=lock_ttl):
            return False
        _SCAN_THREAD = threading.Thread(
            target=_scan_thread_body,
            kwargs={"timeout_seconds": timeout_seconds},
            daemon=True,
            name="keyflip-scan",
        )
        _SCAN_THREAD.start()
        return True


def start_background_scan_if_needed(payload: dict[str, Any] | None) -> bool:
    """Auto-scan when missing or stale. Returns True if scan was started."""
    enabled = os.getenv("AUTO_SCAN_ON_DASHBOARD_START", "1").strip().lower() in {"1", "true", "yes", "y", "on"}
    if not enabled:
        return False

    stale_hours = float(os.getenv("AUTO_SCAN_STALE_AFTER_HOURS", "6"))
    if payload is None:
        return trigger_background_scan(force=False)

    age = scan_age_seconds(payload)
    if age is None:
        return False
    if age > stale_hours * 3600:
        return trigger_background_scan(force=False)
    return False

