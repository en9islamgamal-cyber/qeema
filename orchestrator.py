"""
orchestrator.py — QEEMA v4.0 (Enterprise Production Upgrade)
Full Refactor + Reliability + Parallel Pipeline + Self-Healing
"""

from __future__ import annotations

import json
import logging
import shutil
import traceback
import time
import os
import random
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from supabase import create_client, Client

from config import APIKeys, DBConfig, Paths
from models import EpisodeScript, EpisodeStatus
from script_engine import ScriptEngine
from voice_engine_v2 import VoiceEngine
from visual_engine import VisualEngine
from sfx_engine import SFXEngine
from gamification_engine import GamificationEngine
from video_engine import VideoEngine
from thumbnail_engine import ThumbnailEngine
from quality_gate import QualityGate

logger = logging.getLogger("QEEMA.Orchestrator")


# ───────────────────────────── METRICS ─────────────────────────────

@dataclass
class PipelineMetrics:
    episode_number: int
    script_time: float = 0.0
    audio_time: float = 0.0
    visual_time: float = 0.0
    video_time: float = 0.0
    gamification_time: float = 0.0
    thumbnail_time: float = 0.0
    upload_time: float = 0.0
    total_time: float = 0.0
    failure: bool = False


# ───────────────────────────── UTILITIES ─────────────────────────────

def backoff(attempt: int) -> float:
    """Exponential backoff with jitter"""
    base = 2 ** attempt
    jitter = random.uniform(0, base * 0.3)
    return min(60, base + jitter)


def sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, default=str).encode()).hexdigest()


# ───────────────────────────── ORCHESTRATOR ─────────────────────────────

class PipelineOrchestrator:

    def __init__(self):
        self.db: Optional[Client] = None
        self.quality_gate = QualityGate()
        self._init_supabase()
        self._init_engines()
        Paths.ensure_all()

    # ───────── INIT ─────────

    def _init_supabase(self):
        try:
            self.db = create_client(APIKeys.SUPABASE_URL, APIKeys.SUPABASE_KEY)
            logger.info("✅ DB Connected")
        except Exception as e:
            raise RuntimeError("DB connection failed") from e

    def _init_engines(self):
        self.script = ScriptEngine()
        self.voice = VoiceEngine()
        self.visual = VisualEngine()
        self.sfx = SFXEngine()
        self.gamify = GamificationEngine()
        self.video = VideoEngine()
        self.thumbnail = ThumbnailEngine()
        logger.info("✅ Engines ready")

    # ───────── DB SAFE WRAPPER ─────────

    def _safe_db(self, fn, retries=3):
        for i in range(retries):
            try:
                return fn()
            except Exception as e:
                logger.warning(f"DB retry {i+1}: {e}")
                time.sleep(backoff(i))
        raise RuntimeError("DB operation failed after retries")

    # ───────── EPISODE HANDLING ─────────

    def _get_pending(self):
        return self._safe_db(
            lambda: self.db.table(DBConfig.TABLE_EPISODES)
            .select("*").eq("status", "pending")
            .order("episode_number").limit(1).execute()
            .data
        )

    def _update_episode(self, ep_id: str, **fields):
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._safe_db(
            lambda: self.db.table(DBConfig.TABLE_EPISODES)
            .update(fields).eq("id", ep_id).execute()
        )

    # ───────── SCRIPT ─────────

    def _stage_script(self, ep_num: int) -> EpisodeScript:
        cached = self.script.load_from_disk(ep_num)
        if cached:
            return cached

        script = self.script.generate(ep_num)
        self._save_script(script)
        return script

    def _save_script(self, script: EpisodeScript):
        path = Paths.SCRIPT_DIR / f"ep_{script.episode_number:03d}.json"
        path.write_text(script.model_dump_json(indent=2), encoding="utf-8")

    # ───────── PARALLEL STAGES ─────────

    def _stage_audio(self, script, ep_dir):
        return self.voice.generate_episode_audio(script, ep_dir)

    def _stage_visuals(self, script, ep_dir):
        return self.visual.generate_episode_visuals(script, ep_dir)

    # ───────── PIPELINE EXECUTOR ─────────

    def _run_parallel(self, script, ep_dir):
        results = {}

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {
                ex.submit(self._stage_audio, script, ep_dir): "audio",
                ex.submit(self._stage_visuals, script, ep_dir): "visuals",
            }

            for f in as_completed(futures):
                key = futures[f]
                try:
                    results[key] = f.result()
                except Exception as e:
                    logger.error(f"{key} failed: {e}")
                    raise

        return results

    # ───────── VIDEO PIPELINE ─────────

    def _stage_video(self, script, ep_dir):
        return self.video.assemble_episode(script, ep_dir)

    def _stage_gamification(self, raw_path, script):
        return self.gamify.apply_to_episode(
            raw_path,
            script,
            str(Paths.VIDEOS / f"ep_{script.episode_number:03d}_final.mp4")
        )

    def _stage_thumbnail(self, script):
        return self.thumbnail.create(
            script,
            script.episode_number,
            script.intro_scene.image_path
        )

    # ───────── CLEANUP ─────────

    def _cleanup(self, ep_dir):
        seg = Path(ep_dir) / "segments"
        if seg.exists():
            shutil.rmtree(seg, ignore_errors=True)

    # ───────── MAIN RUN ─────────

    def run(self, episode_number: Optional[int] = None):

        metrics = PipelineMetrics(episode_number=episode_number or 0)
        start = time.time()

        try:
            pending = self._get_pending()
            if not pending and episode_number is None:
                logger.info("No episodes")
                return

            ep = pending[0] if episode_number is None else {"episode_number": episode_number, "id": "manual"}
            ep_id = ep["id"]
            ep_num = ep["episode_number"]

            self._update_episode(ep_id, status=EpisodeStatus.PROCESSING)

            ep_dir = Paths.TEMP_EPISODES / f"ep_{ep_num:03d}"
            ep_dir.mkdir(parents=True, exist_ok=True)

            # SCRIPT
            t = time.time()
            script = self._stage_script(ep_num)
            metrics.script_time = time.time() - t
            script.episode_id = ep_id

            # QUALITY CHECK
            report = self.quality_gate.evaluate(script.model_dump())
            if not report.passed:
                script = self.script.generate(ep_num)

            # PARALLEL AUDIO + VISUALS
            t = time.time()
            parallel = self._run_parallel(script, ep_dir)
            metrics.audio_time = metrics.visual_time = time.time() - t

            # VIDEO
            t = time.time()
            raw = self._stage_video(script, ep_dir)
            metrics.video_time = time.time() - t

            # GAMIFICATION
            t = time.time()
            final = self._stage_gamification(raw, script)
            metrics.gamification_time = time.time() - t

            # THUMBNAIL
            t = time.time()
            thumb = self._stage_thumbnail(script)
            metrics.thumbnail_time = time.time() - t

            # CLEANUP
            self._cleanup(ep_dir)

            self._update_episode(
                ep_id,
                status=EpisodeStatus.COMPLETED,
                video_path=final
            )

            metrics.total_time = time.time() - start

            logger.info(f"✅ Episode {ep_num} done in {metrics.total_time:.2f}s")

        except Exception as e:
            metrics.failure = True
            logger.error(traceback.format_exc())

            if 'ep_id' in locals():
                self._update_episode(ep_id, status=EpisodeStatus.FAILED, error=str(e))

            raise