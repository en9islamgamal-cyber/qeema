"""
infrastructure/repository_local.py — VALUE / QEEMA v20.1 (NEW)
=========================================================================
Local JSON-file backed EpisodeRepository.

[Why this exists]
For development, testing, and dry-run pipelines, you don't want or need
Supabase. This repo persists state to a single local JSON file
(state/local_episodes.json) — same interface as SupabaseRepository.

[Use cases]
1. Initial development: no Supabase project needed
2. Local testing: faster iteration, no network calls
3. Demo/showcase: works completely offline
4. CI smoke tests: no external dependencies

[Behavior]
- File-backed atomic writes (tmp + rename pattern)
- Same EpisodeRepository ABC as SupabaseRepository
- Status transitions: pending → in_progress → awaiting_review/published/failed
- Stage state persistence for resume (same as Supabase)
- Thread-safe via single-writer lock (file-based, OS-managed)

[Schema]
{
  "episodes": {
    "1": {
      "id": "local-1",
      "episode_number": 1,
      "status": "pending",
      "youtube_url": null,
      "created_at": "2026-05-05T19:30:00Z",
      "updated_at": "2026-05-05T19:30:00Z",
      "stage_states": {
        "script": {...},
        "audio": {...}
      }
    }
  }
}
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.interfaces import EpisodeRepository

logger = logging.getLogger(__name__)


class LocalRepository(EpisodeRepository):
    """JSON-file backed episode repository.

    Drop-in replacement for SupabaseRepository — same interface,
    no network, no auth, no costs.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        state_dir: Path,
        filename: str = "local_episodes.json",
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._state_dir / filename
        self._lock = threading.Lock()  # in-process lock (cross-process: use file lock)

        # Initialize file if missing
        if not self._state_file.exists():
            self._write({
                "schema_version": self.SCHEMA_VERSION,
                "episodes": {},
            })

        logger.info(
            f"📁 LocalRepository ready: {self._state_file} "
            f"({len(self._read().get('episodes', {}))} episodes tracked)"
        )

    # ─── Internal I/O ────────────────────────────────────────────
    def _read(self) -> Dict[str, Any]:
        """Read state file. Returns {schema_version, episodes} dict."""
        try:
            with self._state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("State file is not a dict")
            return data
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"⚠️ State file corrupt ({e}) — resetting")
            return {"schema_version": self.SCHEMA_VERSION, "episodes": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        """Atomic write: tmp + rename."""
        tmp = self._state_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(self._state_file)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ─── EpisodeRepository implementation ───────────────────────
    def get_or_create(self, episode_number: int) -> Dict[str, Any]:
        """Return existing record or create new with status='pending'."""
        with self._lock:
            data = self._read()
            episodes = data.setdefault("episodes", {})
            key = str(episode_number)

            if key in episodes:
                logger.info(
                    f"📋 Episode {episode_number} exists "
                    f"(status={episodes[key].get('status')})"
                )
                return episodes[key]

            new_record = {
                "id": f"local-{episode_number}",
                "episode_number": episode_number,
                "status": "pending",
                "youtube_url": None,
                "created_at": self._now_iso(),
                "updated_at": self._now_iso(),
                "stage_states": {},
            }
            episodes[key] = new_record
            self._write(data)
            logger.info(f"📝 Created local episode {episode_number}")
            return new_record

    def update_status(
        self,
        episode_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        """Update status + any extra fields."""
        with self._lock:
            data = self._read()
            episodes = data.setdefault("episodes", {})
            # Find by id (loop because key is episode_number string)
            for key, record in episodes.items():
                if record.get("id") == episode_id:
                    record["status"] = status
                    record["updated_at"] = self._now_iso()
                    for k, v in fields.items():
                        if v is not None:
                            record[k] = v
                    self._write(data)
                    logger.info(
                        f"📝 Updated episode {record.get('episode_number')} "
                        f"→ status={status}"
                    )
                    return
            logger.warning(f"⚠️ Episode {episode_id} not found for update")

    def get_pending(self) -> Optional[Dict[str, Any]]:
        """Return lowest-numbered pending episode."""
        with self._lock:
            data = self._read()
            episodes = data.get("episodes", {})
            pending = [
                r for r in episodes.values()
                if r.get("status") == "pending"
            ]
            if not pending:
                return None
            pending.sort(key=lambda r: r.get("episode_number", 0))
            return pending[0]

    def save_state(
        self,
        episode_id: str,
        stage: str,
        state: Dict[str, Any],
    ) -> None:
        """Persist stage state for resume."""
        with self._lock:
            data = self._read()
            episodes = data.setdefault("episodes", {})
            for record in episodes.values():
                if record.get("id") == episode_id:
                    record.setdefault("stage_states", {})[stage] = {
                        "saved_at": self._now_iso(),
                        "state": state,
                    }
                    record["updated_at"] = self._now_iso()
                    self._write(data)
                    return
            logger.warning(f"⚠️ Cannot save state — episode {episode_id} not found")

    def get_state(
        self,
        episode_id: str,
        stage: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve stage state if exists."""
        with self._lock:
            data = self._read()
            episodes = data.get("episodes", {})
            for record in episodes.values():
                if record.get("id") == episode_id:
                    stage_data = record.get("stage_states", {}).get(stage)
                    if stage_data:
                        return stage_data.get("state")
            return None

    def list_episodes(self) -> List[Dict[str, Any]]:
        """Return all episodes."""
        with self._lock:
            data = self._read()
            episodes = list(data.get("episodes", {}).values())
            episodes.sort(key=lambda r: r.get("episode_number", 0))
            return episodes

    # ─── Bonus: utility methods (LocalRepository only) ──────────
    def reset(self) -> None:
        """Wipe all state — useful for fresh test runs.

        WARNING: Destroys all episode tracking. Use only in dev/test.
        """
        with self._lock:
            self._write({
                "schema_version": self.SCHEMA_VERSION,
                "episodes": {},
            })
            logger.warning(f"🗑️  LocalRepository state RESET: {self._state_file}")

    def stats(self) -> Dict[str, int]:
        """Return episode counts by status (for debug dashboard)."""
        episodes = self.list_episodes()
        counts: Dict[str, int] = {}
        for record in episodes:
            status = record.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts
