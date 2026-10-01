"""Tiny client for the workshop chaos-controller.

Every service polls GET {CHAOS_URL}/flags every ~2s in a background thread and
keeps the last good copy in memory, so a request never waits on the controller.
The poll uses urllib so it can be excluded from tracing with
OTEL_PYTHON_URLLIB_EXCLUDED_URLS=chaos-controller (see docker-compose.yml).
"""

import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("chaos")


class ChaosFlags:
    def __init__(self, url: str | None = None, interval: float = 2.0):
        base = (url or os.getenv("CHAOS_URL", "http://localhost:8090")).rstrip("/")
        self.url = f"{base}/flags"
        self.interval = interval
        self._flags: dict = {}
        self._lock = threading.Lock()
        self._last_error = None
        threading.Thread(target=self._loop, name="chaos-poller", daemon=True).start()

    def _loop(self):
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode())
                with self._lock:
                    self._flags = data
                if self._last_error:
                    log.info("chaos-controller reachable again")
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001 - never let chaos polling break the app
                if str(exc) != self._last_error:
                    log.warning("chaos-controller unreachable (%s); keeping last flags", exc)
                    self._last_error = str(exc)
            time.sleep(self.interval)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._flags)

    def enabled(self, name: str) -> bool:
        with self._lock:
            return bool(self._flags.get(name, {}).get("enabled", False))

    def param(self, name: str, key: str, default):
        with self._lock:
            value = self._flags.get(name, {}).get("params", {}).get(key, default)
        # coerce to the default's type so callers can trust the value
        try:
            if isinstance(default, bool):
                return str(value).lower() in ("1", "true", "yes", "on")
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
        except (TypeError, ValueError):
            return default
        return value
