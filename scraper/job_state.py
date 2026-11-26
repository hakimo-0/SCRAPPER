# -*- coding: utf-8 -*-
"""Persistent job state for pause / resume across Streamlit reruns and PC restarts.

One JSON file per source is kept under ``output/job_states/``.  It stores the
job parameters, every lead row (with ``_status`` marking done / pending), the
processed count and the current status.  When the PC is closed or the browser
dies, the file remains and the next app start can offer "Resume".
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent.parent / "output" / "job_states"
STATE_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _path(source_id: str) -> Path:
    return STATE_DIR / f"{source_id}.json"


def save(source_id: str, data: dict) -> None:
    data["updated_at"] = datetime.now().isoformat()
    path = _path(source_id)
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def load(source_id: str) -> dict | None:
    path = _path(source_id)
    if not path.exists():
        return None
    try:
        with _lock:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        # A failed/interrupted write should not prevent the dashboard from
        # starting.  Normal writes are atomic; this is a last-resort guard for
        # manually edited or legacy state files.
        return None


def clear(source_id: str) -> None:
    path = _path(source_id)
    if path.exists():
        with _lock:
            path.unlink()


def list_states() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in STATE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[path.stem] = data
        except Exception:
            continue
    return out
