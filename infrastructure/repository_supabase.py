"""
infrastructure/repository_supabase.py — VALUE / QEEMA v22.5 — Supabase episode repository
============================================================================
Supabase implementation of EpisodeRepository.

[Design]
- Defensive: every DB call wrapped in try/except + logged
- Idempotent: get_or_create won't double-insert
- Retry: brief network blips trigger retry (handled by client)
- ASCII-only status values (DB columns are case-sensitive)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.exceptions import ConfigurationError, NetworkError
from core.interfaces import EpisodeRepository
from core.models import EpisodeStatus

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════
TABLE_EPISODES: str = "episodes"
TABLE_PIPELINE_STATE: str = "pipeline_state"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_status(status: Any) -> str:
    """Always store statuses as lowercase ASCII strings."""
    if hasattr(status, "value"):
        status = status.value
    return str(status).lower()


# ════════════════════════════════════════════════════════════════
# SupabaseRepository
# ════════════════════════════════════════════════════════════════
class SupabaseRepository(EpisodeRepository):
    """Episode persistence backed by Supabase (Postgres)."""

    def __init__(self, url: str, key: str) -> None:
        if not url or not key:
            raise ConfigurationError(
                "SupabaseRepository requires both url and key"
            )
        try:
            from supabase import create_client, Client  # type: ignore
        except ImportError as e:
            raise RuntimeError("supabase-py not installed") from e

        try:
            self._client: "Client" = create_client(url, key)
            # Connectivity probe
            self._client.table(TABLE_EPISODES).select(
                "count", count="exact"
            ).limit(0).execute()
            logger.info("✅ Supabase connected")
        except Exception as e:
            raise NetworkError(f"Failed to connect to Supabase: {e}", cause=e) from e

    # ───────────────────────────────────────────────────────────
    # CRUD
    # ───────────────────────────────────────────────────────────
    def get_or_create(self, episode_number: int) -> Dict[str, Any]:
        existing = self._fetch_one(episode_number)
        if existing:
            return existing

        now = _utc_now_iso()
        try:
            res = (
                self._client.table(TABLE_EPISODES)
                .insert({
                    "episode_number": episode_number,
                    "status": EpisodeStatus.PENDING.value,
                    "created_at": now,
                    "updated_at": now,
                })
                .execute()
            )
        except Exception as e:
            raise NetworkError(
                f"Supabase insert failed (episode {episode_number}): {e}",
                cause=e,
            ) from e

        if not res.data:
            raise NetworkError(
                f"Supabase insert returned empty for episode {episode_number}"
            )
        return res.data[0]

    def update_status(
        self,
        episode_id: str,
        status: str,
        **fields: Any,
    ) -> None:
        payload: Dict[str, Any] = dict(fields)
        payload["status"] = _coerce_status(status)
        payload["updated_at"] = _utc_now_iso()
        # Strip fields the table doesn't accept
        payload.pop("error", None)

        try:
            (
                self._client.table(TABLE_EPISODES)
                .update(payload)
                .eq("id", episode_id)
                .execute()
            )
        except Exception as e:
            # Don't raise; status updates shouldn't crash the pipeline
            logger.warning(
                f"⚠️ Supabase update failed (id={episode_id}): {e}"
            )

    def get_pending(self) -> Optional[Dict[str, Any]]:
        try:
            res = (
                self._client.table(TABLE_EPISODES)
                .select("*")
                .ilike("status", "pending")
                .order("episode_number")
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"❌ Failed to fetch pending episode: {e}")
            return None

    def save_state(
        self,
        episode_id: str,
        stage: str,
        state: Dict[str, Any],
    ) -> None:
        try:
            (
                self._client.table(TABLE_PIPELINE_STATE)
                .upsert(
                    {
                        "episode_id": episode_id,
                        "stage": stage,
                        "state_data": json.dumps(state, ensure_ascii=False),
                        "updated_at": _utc_now_iso(),
                    },
                    on_conflict="episode_id,stage",
                )
                .execute()
            )
        except Exception as e:
            logger.warning(
                f"⚠️ save_state failed (id={episode_id}, stage={stage}): {e}"
            )

    def get_state(
        self,
        episode_id: str,
        stage: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            res = (
                self._client.table(TABLE_PIPELINE_STATE)
                .select("state_data")
                .eq("episode_id", episode_id)
                .eq("stage", stage)
                .limit(1)
                .execute()
            )
            if not res.data:
                return None
            raw = res.data[0].get("state_data")
            if not raw:
                return None
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.warning(
                f"⚠️ get_state failed (id={episode_id}, stage={stage}): {e}"
            )
            return None

    def list_episodes(self) -> List[Dict[str, Any]]:
        try:
            res = (
                self._client.table(TABLE_EPISODES)
                .select("*")
                .order("episode_number")
                .execute()
            )
            return list(res.data) if res.data else []
        except Exception as e:
            logger.error(f"❌ list_episodes failed: {e}")
            return []

    # ───────────────────────────────────────────────────────────
    # Internal
    # ───────────────────────────────────────────────────────────
    def _fetch_one(self, episode_number: int) -> Optional[Dict[str, Any]]:
        try:
            res = (
                self._client.table(TABLE_EPISODES)
                .select("*")
                .eq("episode_number", episode_number)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(
                f"❌ fetch episode {episode_number} failed: {e}"
            )
            return None
