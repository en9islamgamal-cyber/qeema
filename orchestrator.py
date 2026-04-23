"""
orchestrator.py — VALUE / QEEMA v2
═══════════════════════════════════════════════════════
قائد المنظومة الكاملة
• استئناف من أي مرحلة عند الانقطاع
• حفظ الحالة في Supabase بعد كل خطوة
• معالجة استثنائية شاملة
═══════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import traceback
import time  # تمت الإضافة هنا من أجل تأخير إعادة المحاولة أثناء الرفع
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus, PipelineState
from script_engine  import ScriptEngine
from voice_engine_v2 import VoiceEngine
from visual_engine  import VisualEngine
from sfx_engine     import SFXEngine
from gamification_engine import GamificationEngine
from video_engine   import VideoEngine
from thumbnail_engine import ThumbnailEngine

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """
    ينفّذ خط الإنتاج الكامل مع:
    - استئناف ذكي من آخر نقطة
    - حفظ تدريجي في Supabase
    - معالجة أخطاء شاملة
    """

    def __init__(self):
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    def _init_supabase(self):
        self.db: Client = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
        logger.info("✅ Supabase متصل")

    def _init_engines(self):
        logger.info("🔧 تهيئة المحركات…")
        self.script    = ScriptEngine()
        self.voice     = VoiceEngine()
        self.visual    = VisualEngine()
        self.sfx       = SFXEngine()
        self.gamify    = GamificationEngine()
        self.video     = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ جميع المحركات جاهزة")

    # ──────────────────────────── Supabase ─────
    def _db_get_next(self) -> Optional[dict]:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("*")
             .eq("status", "pending")
             .order("episode_number")
             .limit(1)
             .execute())
        return r.data[0] if r.data else None

    def _db_update(self, ep_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()

        if "status" in fields and hasattr(fields["status"], "value"):
            fields["status"] = fields["status"].value.lower()

        try:
            self.db.table(DBConfig.TABLE_EPISODES).update(fields).eq("id", ep_id).execute()
        except Exception as e:
            logger.warning(f"⚠️ DB update فشل: {e}")

    def _db_init_episode(self, ep_num: int) -> str:
        r = (self.db.table(DBConfig.TABLE_EPISODES)
             .select("id,status")
             .eq("episode_number", ep_num)
             .execute())
        if r.data:
            return r.data[0]["id"]

        res = self.db.table(DBConfig.TABLE_EPISODES).insert({
            "episode_number": ep_num,
            "status": "pending",
            "created_at":
