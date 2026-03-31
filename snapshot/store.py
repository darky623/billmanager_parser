"""Snapshot storage for raw pricelist JSON per provider.

Used for change detection: compare raw JSON from provider
before making any API calls to cloudsell.
"""

import hashlib
import json
import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class SnapshotStore:
    def __init__(self, base_dir: str = "snapshots") -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _provider_dir(self, provider_host: str) -> Path:
        safe = provider_host.replace("https://", "").replace("http://", "").replace("/", "_")
        path = self._base / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _plan_path(self, provider_host: str, plan_id: int) -> Path:
        return self._provider_dir(provider_host) / f"{plan_id}.json"

    def load(self, provider_host: str, plan_id: int) -> dict | None:
        """Load previous snapshot for a plan. Returns None if not exists."""
        path = self._plan_path(provider_host, plan_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_bytes())
        except Exception as exc:
            log.warning("Snapshot: failed to read", path=str(path), error=str(exc))
            return None

    def save(self, provider_host: str, plan_id: int, data: dict) -> None:
        """Persist current plan snapshot to disk."""
        path = self._plan_path(provider_host, plan_id)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            log.error("Snapshot: failed to write", path=str(path), error=str(exc))

    def has_changed(self, provider_host: str, plan_id: int, current: dict) -> bool:
        """
        Return True if the plan data has changed since last snapshot.
        Comparison is done on a canonical (sorted-keys) JSON string hash.
        Returns True when no previous snapshot exists (treat as new).
        """
        previous = self.load(provider_host, plan_id)
        if previous is None:
            log.debug("Snapshot: no previous snapshot (new plan)", plan_id=plan_id)
            return True

        prev_hash = _hash(previous)
        curr_hash = _hash(current)

        if prev_hash != curr_hash:
            log.debug("Snapshot: plan changed", plan_id=plan_id)
            return True

        log.debug("Snapshot: plan unchanged", plan_id=plan_id)
        return False

    def list_saved_ids(self, provider_host: str) -> set[int]:
        """Return all plan IDs that have a saved snapshot for this provider."""
        directory = self._provider_dir(provider_host)
        ids: set[int] = set()
        for f in directory.iterdir():
            if f.suffix == ".json":
                try:
                    ids.add(int(f.stem))
                except ValueError:
                    pass
        return ids

    def remove(self, provider_host: str, plan_id: int) -> None:
        """Remove snapshot (plan deactivated)."""
        path = self._plan_path(provider_host, plan_id)
        if path.exists():
            path.unlink()
            log.debug("Snapshot: removed", plan_id=plan_id)


def _hash(data: dict) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def plan_to_snapshot_dict(plan_id: int, prices: list[dict], detail: dict) -> dict:
    """Build a lightweight snapshot dict for comparison (not full raw response)."""
    return {
        "plan_id": plan_id,
        "prices": sorted(prices, key=lambda p: p["period"]),
        "detail": detail,
    }
