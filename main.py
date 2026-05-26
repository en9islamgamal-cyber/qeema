#!/usr/bin/env python3
"""
main.py
====================================================================
QEEMA v2 — single-phase episode pipeline.

Runs end-to-end for ONE episode:
  1. Generate script (2 Gemini calls)
  2. Generate visuals (Leonardo)
  3. Generate narration audio (ElevenLabs)
  4. Download tilawah (everyayah)
  5. Mix audio (FFmpeg)
  6. Assemble video (FFmpeg)
  7. Build thumbnails
  8. Upload to YouTube (optional)

Usage:
    python main.py --episode 3                  # full run
    python main.py --episode 3 --dry-run        # skip YouTube
    python main.py --episode 3 --force          # ignore caches
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Logging configured before any imports that might log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qeema")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="QEEMA v2 — Generate one Quranic episode for kids",
    )
    parser.add_argument(
        "--episode", "-e", type=int, required=True,
        help="Episode number from data/curriculum.py (1-38)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run everything except YouTube upload",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore caches and regenerate everything",
    )
    parser.add_argument(
        "--skip-script", action="store_true",
        help="Skip script generation (use cached bundle)",
    )
    parser.add_argument(
        "--skip-assets", action="store_true",
        help="Skip asset generation (Leonardo, ElevenLabs, tilawah)",
    )
    parser.add_argument(
        "--skip-video", action="store_true",
        help="Skip video assembly",
    )

    args = parser.parse_args()

    # Imports inside main() so logging is set up first
    from core.config import (
        ensure_runtime_dirs, EPISODES_DIR, LOGO_PATH, get_api_keys,
    )
    from core.models import EpisodeRequest
    from data.curriculum import get_episode_info

    ensure_runtime_dirs()

    # Validate logo exists (required for video assembly)
    if not LOGO_PATH.exists():
        log.warning(
            "⚠️  Logo not found at %s — video will be assembled without "
            "watermark/intro splash. Place your channel logo there before "
            "running production builds.",
            LOGO_PATH,
        )

    # Validate env vars early
    try:
        keys = get_api_keys()
        log.info(
            "✓ API keys loaded: %d Gemini keys, ElevenLabs, Leonardo, "
            "Supabase, YouTube",
            len(keys.gemini_keys_list()),
        )
    except RuntimeError as e:
        log.error("❌ %s", e)
        return 2

    # Look up episode
    ep_info = get_episode_info(args.episode)
    if not ep_info:
        log.error(
            "❌ Episode %d not found in curriculum. Valid: 1-38",
            args.episode,
        )
        return 2

    request = EpisodeRequest(
        episode_number=ep_info.episode_number,
        surah_number=ep_info.surah_number,
        surah_name=ep_info.surah_name,
        start_ayah=ep_info.start_ayah,
        end_ayah=ep_info.end_ayah,
    )

    log.info("━" * 60)
    log.info(
        "🌙 QEEMA v2 — Episode %d: سورة %s (آيات %d-%d)",
        request.episode_number, request.surah_name,
        request.start_ayah, request.end_ayah,
    )
    log.info("━" * 60)

    started_at = datetime.now()

    # ─── PHASE 1: SCRIPT ─────────────────────────────────────────
    if not args.skip_script:
        log.info("📝 PHASE 1/4: Script generation")
        from pipeline.orchestrator import ScriptOrchestrator
        orch = ScriptOrchestrator()
        bundle = orch.generate(request, force=args.force)
    else:
        log.info("📝 PHASE 1/4: Loading cached script bundle")
        from core.models import EpisodeBundle
        bundle_path = (
            EPISODES_DIR / f"episode_{request.episode_number:03d}" / "bundle.json"
        )
        if not bundle_path.exists():
            log.error("❌ No cached bundle at %s", bundle_path)
            return 2
        bundle = EpisodeBundle.model_validate_json(
            bundle_path.read_text(encoding="utf-8")
        )

    log.info("✅ Script ready: title='%s'", bundle.narration.title)

    if args.skip_assets and args.skip_video:
        log.info(
            "Skip flags set; stopping after script. "
            "Bundle saved at: state/episodes/episode_%03d/bundle.json",
            request.episode_number,
        )
        return 0

    # ─── PHASE 2: ASSETS ────────────────────────────────────────
    episode_dir = EPISODES_DIR / f"episode_{request.episode_number:03d}"
    episode_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_assets:
        log.info("🎨 PHASE 2/4: Generating assets")
        leonardo_images, audio_paths, tilawah_full = _generate_assets(
            bundle, episode_dir,
        )
    else:
        log.info("🎨 PHASE 2/4: Using existing assets")
        leonardo_images, audio_paths, tilawah_full = _locate_existing_assets(
            episode_dir,
        )

    # ─── PHASE 3: VIDEO ASSEMBLY ────────────────────────────────
    final_video = episode_dir / f"episode_{request.episode_number:03d}.mp4"
    thumbnails: list = []

    if not args.skip_video:
        log.info("🎬 PHASE 3/4: Assembling video")
        final_video, thumbnails = _assemble_video(
            bundle, leonardo_images, audio_paths, tilawah_full,
            final_video, episode_dir,
        )
    else:
        log.info("🎬 PHASE 3/4: Skipping video assembly")

    # ─── PHASE 4: YOUTUBE UPLOAD ────────────────────────────────
    if args.dry_run:
        log.info("📤 PHASE 4/4: Dry run — skipping YouTube upload")
        log.info("Final video: %s", final_video)
        return 0

    if not final_video.exists():
        log.error("❌ Final video missing, can't upload")
        return 3

    log.info("📤 PHASE 4/4: Uploading to YouTube")
    from publishing.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader()

    # Use first thumbnail variant for upload; YouTube allows changing later
    primary_thumb = thumbnails[0] if thumbnails else None

    result = uploader.upload(
        video_path=final_video,
        title=bundle.narration.youtube_title,
        description=bundle.narration.youtube_description,
        tags=bundle.narration.youtube_tags,
        thumbnail_path=primary_thumb,
    )

    elapsed = datetime.now() - started_at
    log.info("━" * 60)
    log.info("✅ EPISODE %d DONE — %s", request.episode_number, result.video_url)
    log.info("   Total time: %s", elapsed)
    log.info("━" * 60)
    return 0


# ════════════════════════════════════════════════════════════════════
# Asset generation helpers
# ════════════════════════════════════════════════════════════════════

def _generate_assets(bundle, episode_dir):
    """Generate Leonardo images + ElevenLabs audio + tilawah download."""
    from assets_engines.leonardo_client import LeonardoClient
    from assets_engines.elevenlabs_client import ElevenLabsClient
    from assets_engines.tilawah_fetcher import (
        fetch_tilawah_for_episode, concat_tilawah_files,
    )
    from pipeline.prompts import UNIFIED_VISUAL_NEGATIVE

    # ── Leonardo: all images ──────────────────────────────────────
    log.info("→ Leonardo: generating images...")
    leo = LeonardoClient()

    # Collect all prompts: hook, intro, ayahs..., outro, thumbnails
    all_visuals = [
        ("hook", bundle.hook_and_visuals.hook_visual),
        ("intro", bundle.hook_and_visuals.intro_visual),
    ]
    for i, v in enumerate(bundle.hook_and_visuals.ayah_visuals, start=1):
        all_visuals.append((f"ayah_{i}", v))
    all_visuals.append(("outro", bundle.hook_and_visuals.outro_visual))
    for i, v in enumerate(bundle.hook_and_visuals.thumbnail_visuals, start=1):
        all_visuals.append((f"thumb_{i}", v))

    images_by_label = {}
    for label, vp in all_visuals:
        log.info("  Generating %s image...", label)
        result = leo.generate(
            prompt=vp.full_prompt,
            negative_prompt=UNIFIED_VISUAL_NEGATIVE,
        )
        images_by_label[label] = result.local_path

    # ── ElevenLabs: 5 narration segments ──────────────────────────
    log.info("→ ElevenLabs: synthesizing narration...")
    el = ElevenLabsClient()

    # Combine all ayah narrations into one long explanation
    explanation_text = " ".join(
        a.narration.strip() for a in bundle.narration.ayahs
    )

    audio_paths = {
        "hook":        el.synthesize(
            bundle.hook_and_visuals.hook_text, label="hook"
        ).local_path,
        "intro":       el.synthesize(
            bundle.narration.intro, label="intro"
        ).local_path,
        "explanation": el.synthesize(
            explanation_text, label="explanation"
        ).local_path,
        "transition":  el.synthesize(
            bundle.narration.transition_to_second_recitation, label="transition"
        ).local_path,
        "outro":       el.synthesize(
            bundle.narration.outro, label="outro"
        ).local_path,
    }

    # ── Tilawah: download per-ayah MP3s, concat into one file ─────
    log.info("→ Tilawah: downloading Husary recitation...")
    tilawah_files = fetch_tilawah_for_episode(
        surah_number=bundle.surah_number,
        start_ayah=bundle.start_ayah,
        end_ayah=bundle.end_ayah,
        include_basmala=(bundle.start_ayah == 1),
    )
    tilawah_full = episode_dir / "tilawah_full.mp3"
    concat_tilawah_files(tilawah_files, tilawah_full)

    return images_by_label, audio_paths, tilawah_full


def _locate_existing_assets(episode_dir):
    """When --skip-assets, locate existing files by convention."""
    raise NotImplementedError(
        "Asset relocation from cache not implemented yet. "
        "Don't use --skip-assets unless you know where files are."
    )


# ════════════════════════════════════════════════════════════════════
# Video assembly helper
# ════════════════════════════════════════════════════════════════════

def _assemble_video(
    bundle, leonardo_images, audio_paths, tilawah_full,
    final_video, episode_dir,
):
    """Build the final MP4 + thumbnails."""
    from video.audio_director import build_episode_audio
    from video.video_assembler import build_episode_video, VideoScene
    from video.thumbnail_builder import build_thumbnails_batch

    # ── Mix audio ────────────────────────────────────────────────
    log.info("→ Mixing episode audio...")
    mixed = build_episode_audio(
        hook_audio=audio_paths["hook"],
        intro_audio=audio_paths["intro"],
        explanation_audio=audio_paths["explanation"],
        transition_audio=audio_paths["transition"],
        outro_audio=audio_paths["outro"],
        tilawah_full=tilawah_full,
        output=episode_dir / "audio_mixed.mp3",
    )
    log.info("   Total audio duration: %.1fs", mixed.total_duration_sec)

    # ── Build scenes (image timeline matching audio) ─────────────
    # Order: hook → intro → tilawah_1 → ayah_1..N → transition → tilawah_2 → outro
    # Each segment's duration is in mixed.timeline_seconds (cumulative starts)
    # We need DURATIONS, not start times. Compute from timeline.
    starts = mixed.timeline_seconds + [mixed.total_duration_sec]
    durations = [starts[i+1] - starts[i] for i in range(len(mixed.timeline_seconds))]

    # Segments index in build_episode_audio:
    #   0: hook         → hook_visual
    #   1: intro        → intro_visual
    #   2: tilawah_1    → intro_visual (kept calm)
    #   3: explanation  → ayah_visuals in sequence
    #   4: transition   → outro_visual (gentle transition)
    #   5: tilawah_2    → cycle through ayah_visuals again
    #   6: outro        → outro_visual

    n_ayahs = bundle.ayah_count()
    explanation_dur = durations[3]
    per_ayah_explain = explanation_dur / max(1, n_ayahs)

    tilawah_2_dur = durations[5]
    per_ayah_tilawah_2 = tilawah_2_dur / max(1, n_ayahs)

    scenes = []
    scenes.append(VideoScene(leonardo_images["hook"],  durations[0], "hook"))
    scenes.append(VideoScene(leonardo_images["intro"], durations[1], "intro"))
    scenes.append(VideoScene(leonardo_images["intro"], durations[2], "tilawah_1"))
    # Explanation: split across ayah_visuals
    for i in range(1, n_ayahs + 1):
        scenes.append(VideoScene(
            leonardo_images[f"ayah_{i}"],
            per_ayah_explain,
            f"explain_ayah_{i}",
        ))
    scenes.append(VideoScene(leonardo_images["outro"], durations[4], "transition"))
    # Tilawah 2: cycle ayah images again
    for i in range(1, n_ayahs + 1):
        scenes.append(VideoScene(
            leonardo_images[f"ayah_{i}"],
            per_ayah_tilawah_2,
            f"tilawah2_ayah_{i}",
        ))
    scenes.append(VideoScene(leonardo_images["outro"], durations[6], "outro"))

    # ── Assemble final MP4 ───────────────────────────────────────
    log.info("→ Assembling final video (%d scenes)...", len(scenes))
    build_episode_video(
        scenes=scenes,
        audio_path=mixed.output_path,
        output=final_video,
    )

    # ── Build thumbnails ─────────────────────────────────────────
    log.info("→ Building thumbnails...")
    thumb_sources = [leonardo_images[f"thumb_{i}"] for i in (1, 2, 3)]
    thumbnails = build_thumbnails_batch(thumb_sources, episode_dir / "thumbnails")

    return final_video, thumbnails


# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    sys.exit(main())
