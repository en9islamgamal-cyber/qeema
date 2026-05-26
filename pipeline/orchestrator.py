"""
pipeline/orchestrator.py
====================================================================
The script-generation orchestrator.

Runs:
  Step 1: Fetch verified ayah text from quran.com
  Step 2: Generate narration (Gemini call 1)
  Step 3: Generate hook + visuals (Gemini call 2)
  Step 4: Assemble EpisodeBundle, save to disk

Output: state/episodes/episode_NNN/bundle.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import EPISODES_DIR
from core.models import (
    EpisodeBundle, EpisodeRequest, EpisodeNarration, EpisodeHookAndVisuals,
)
from assets_engines.gemini_client import GeminiClient
from assets_engines.ayah_text_fetcher import fetch_ayahs
from pipeline.tafsir_generator import generate_narration
from pipeline.hook_visuals_generator import generate_hook_and_visuals


log = logging.getLogger(__name__)


class ScriptOrchestrator:
    """Top-level entry point for script generation."""

    def __init__(self, gemini: Optional[GeminiClient] = None) -> None:
        self.gemini = gemini or GeminiClient()

    def generate(
        self,
        request: EpisodeRequest,
        force: bool = False,
    ) -> EpisodeBundle:
        """
        Generate (or load from cache) the script bundle for an episode.

        If a bundle already exists on disk and force=False, returns it
        without re-calling Gemini (huge cost saver).
        """
        episode_dir = EPISODES_DIR / f"episode_{request.episode_number:03d}"
        bundle_path = episode_dir / "bundle.json"

        if bundle_path.exists() and not force:
            log.info(
                "✓ Loading cached bundle: %s", bundle_path,
            )
            return EpisodeBundle.model_validate_json(
                bundle_path.read_text(encoding="utf-8")
            )

        log.info(
            "🚀 Generating script for episode %d (سورة %s, آيات %d-%d)",
            request.episode_number, request.surah_name,
            request.start_ayah, request.end_ayah,
        )

        # ─── Step 1: Verified ayah text ──────────────────────────
        log.info("Step 1/3: Fetching verified ayah text...")
        verified_ayahs = fetch_ayahs(
            surah_number=request.surah_number,
            start_ayah=request.start_ayah,
            end_ayah=request.end_ayah,
        )

        # ─── Step 2: Tafsir / Narration ──────────────────────────
        log.info("Step 2/3: Generating narration (Gemini call 1)...")
        narration, key_1 = generate_narration(
            surah_name=request.surah_name,
            surah_number=request.surah_number,
            ayahs=verified_ayahs,
            gemini=self.gemini,
        )

        # ─── Step 3: Hook + Visuals ──────────────────────────────
        log.info("Step 3/3: Generating hook + visuals (Gemini call 2)...")
        hook_and_visuals, key_2 = generate_hook_and_visuals(
            surah_name=request.surah_name,
            narration=narration,
            gemini=self.gemini,
        )

        # ─── Assemble + save ─────────────────────────────────────
        bundle = EpisodeBundle(
            episode_number=request.episode_number,
            surah_number=request.surah_number,
            surah_name=request.surah_name,
            start_ayah=request.start_ayah,
            end_ayah=request.end_ayah,
            narration=narration,
            hook_and_visuals=hook_and_visuals,
            reciter=request.reciter,
            visual_style=request.visual_style,
            pipeline_version="qeema_v2.0",
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            gemini_calls_used=2,
            gemini_keys_used=[key_1, key_2],
        )

        episode_dir.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(
            bundle.model_dump_json(indent=2),
            encoding="utf-8",
        )

        log.info(
            "✅ Bundle saved: %s (%d ayahs)",
            bundle_path, bundle.ayah_count(),
        )
        return bundle
