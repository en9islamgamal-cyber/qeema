from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus
from script_engine import ScriptEngine
from voice_engine_v2 import VoiceEngine              # ✅ use v2 implementation
from visual_engine import VisualEngine
from sfx_engine import SFXEngine
from gamification_engine import GamificationEngine
from video_engine import VideoEngine
from intro_outro_engine import IntroOutroEngine      # ✅ NEW
from thumbnail_engine import ThumbnailEngine
from quality_gate import QualityGate

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    # ──────────────────────────────────────────────
    # تهيئة
    # ──────────────────────────────────────────────
    def _init_supabase(self) -> None:
        try:
            self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
            self.db.table(DBConfig.TABLE_EPISODES).select("count", count="exact").limit(0).execute()
            logger.info("✅ Supabase متصل")
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Supabase: {e}")
            raise RuntimeError("لا يمكن الاتصال بقاعدة البيانات") from e

    def _init_engines(self) -> None:
        logger.info("🔧 تهيئة المحركات...")
        self.script = ScriptEngine()
        self.voice = VoiceEngine()
        self.visual = VisualEngine()
        self.sfx = SFXEngine()
        self.gamify = GamificationEngine()
        self.video = VideoEngine()
        self.intro_outro = IntroOutroEngine()         # ✅ NEW
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ كل المحركات جاهزة")
