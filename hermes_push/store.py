"""JSON-file token store for hermes-push (Task B2).

Persists the APNs device tokens the iOS app registers, keyed by
``device_token``. The store lives at ``$HERMES_HOME/hermes-push/tokens.json``
(matching the host's per-plugin storage convention, e.g. ``disk-cleanup/``).

Design notes:

* **Injectable base path.** ``TokenStore(base_dir=...)`` lets tests point at a
  tmp dir so the suite never touches the real ``~/.hermes``. ``base_dir`` is the
  ``hermes-push`` directory itself (not ``$HERMES_HOME``); the default resolves
  to ``get_hermes_home() / "hermes-push"``.
* **Atomic writes.** Write to a temp file in the same dir, ``fsync``, then
  ``os.replace`` so a crash mid-write can never corrupt ``tokens.json``.
* **Tolerant reads.** A missing or corrupt file starts empty rather than
  raising — the agent must keep running even if the store was clobbered.
* **Thread-safe enough.** A module-level-per-instance ``RLock`` guards the
  read-modify-write cycle. The agent touches this from a couple of hook /
  request threads, not a high-fanout hot path.

HMAC signing uses a single **shared** secret (``HERMES_PUSH_HMAC_SECRET``, the
same value the gateway is configured with), applied by the sender — NOT a
per-device secret. The store therefore keeps no secret material.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover — plugin may load before constants resolves
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


logger = logging.getLogger(__name__)

_FILE_NAME = "tokens.json"


def _now_iso() -> str:
    """UTC timestamp, second precision, suitable for JSON + comparisons."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_base_dir() -> Path:
    """The on-disk home for this plugin: ``$HERMES_HOME/hermes-push``."""
    return get_hermes_home() / "hermes-push"


class TokenStore:
    """Thread-safe JSON-file store of registered device tokens.

    The on-disk shape is ``{device_token: record}`` where ``record`` holds the
    token, ``apns_env``, ``app_version`` and created/updated timestamps.
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else default_base_dir()
        self._path = self._base_dir / _FILE_NAME
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence ------------------------------------------------------

    def _read(self) -> Dict[str, Dict[str, Any]]:
        """Load the token map, tolerating a missing or corrupt file."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:  # pragma: no cover — unusual FS error
            logger.warning("hermes-push: could not read %s: %s", self._path, exc)
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "hermes-push: %s is corrupt; starting from an empty store",
                self._path,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "hermes-push: %s has unexpected shape (%s); starting empty",
                self._path,
                type(data).__name__,
            )
            return {}
        # Drop any non-dict / mis-keyed entries defensively.
        return {
            str(k): v
            for k, v in data.items()
            if isinstance(v, dict)
        }

    def _write(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Atomically persist the token map (temp file + ``os.replace``)."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=_FILE_NAME + ".",
            suffix=".tmp",
            dir=str(self._base_dir),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover
                pass
            raise

    # -- operations -------------------------------------------------------

    def upsert(
        self,
        *,
        device_token: str,
        apns_env: str,
        app_version: str,
    ) -> Dict[str, Any]:
        """Insert or update a device token; return the stored record.

        ``created_at`` is preserved on update; only ``updated_at`` (and the
        env / version) change.
        """
        with self._lock:
            data = self._read()
            existing = data.get(device_token)
            now = _now_iso()
            if existing is None:
                record = {
                    "device_token": device_token,
                    "apns_env": apns_env,
                    "app_version": app_version,
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                record = dict(existing)
                record["device_token"] = device_token
                record["apns_env"] = apns_env
                record["app_version"] = app_version
                record.setdefault("created_at", now)
                record["updated_at"] = now
            data[device_token] = record
            self._write(data)
            return dict(record)

    def get(self, device_token: str) -> Optional[Dict[str, Any]]:
        """Return the record for ``device_token`` or ``None``."""
        with self._lock:
            record = self._read().get(device_token)
            return dict(record) if record is not None else None

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all stored records (copies)."""
        with self._lock:
            return [dict(r) for r in self._read().values()]

    def remove(self, device_token: str) -> bool:
        """Remove a token; return True if it existed."""
        with self._lock:
            data = self._read()
            if device_token not in data:
                return False
            del data[device_token]
            self._write(data)
            return True

    def prune_invalid(self, device_token: str) -> bool:
        """Prune a token the gateway reported invalid (e.g. APNs 410).

        Currently identical to :meth:`remove`, but named separately so callers
        (and tests) express intent — pruning is a gateway-driven cleanup, not a
        user-initiated unregister.
        """
        return self.remove(device_token)
