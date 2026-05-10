"""
QEEMA v22.7 — Asset Persistence Helpers

Two responsibilities, both currently missing from the pipeline:

  1. PROPAGATION: After Phase 2 generates audio/images, copy the asset paths
     onto the corresponding scene fields (scene.intro_audio, scene.image_path,
     etc). Without this, Phase 3 sees `scene.intro_audio = None` even though
     the file exists under asset_paths["mastered_map"], and the renderer
     blows up with "Audio missing for render".

  2. REHYDRATION: On a new GitHub Actions runner, the absolute paths stored
     in Phase 2's state ("/home/runner/work/qeema/qeema/temp/...") are
     meaningless. After AssetStorage downloads the files into the NEW runner's
     temp dir, every absolute path in asset_paths AND on every scene needs to
     be rewritten to point at the new local copies.

Keep this module pure: no I/O, no network. It just mutates the in-memory
episode/state dicts. Storage download happens in asset_storage.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Path naming conventions
# ──────────────────────────────────────────────────────────────────────────
# These must match what the voice engine / image engine actually write.
# If you change the conventions in those engines, update these in lockstep.

_INTRO_KEY = "intro"
_OUTRO_KEY = "outro"


def _scene_key(scene_id: int) -> str:
    """Compose the asset-map key used for an ayah scene."""
    return f"ayah_{scene_id}"


# ──────────────────────────────────────────────────────────────────────────
# Phase 2 → propagate paths into scene objects
# ──────────────────────────────────────────────────────────────────────────


def propagate_paths_to_scenes(episode: Any, asset_paths: dict[str, Any]) -> dict[str, int]:
    """Copy asset paths from `asset_paths` onto each scene's audio/image fields.

    Call this at the END of Phase 2, after voice_engine has written audio files
    and image_engine has written images, and BEFORE saving phase state.

    Mutates `episode` in place. Returns a stats dict for logging.
    """
    mastered: dict[str, str] = asset_paths.get("mastered_map", {}) or {}
    raw_audio: dict[str, str] = asset_paths.get("audio_map", {}) or {}
    images_dir: str = asset_paths.get("ai_images_dir", "") or ""

    # Prefer the mastered (post-processed) audio. Fall back to raw mp3 if mastering
    # for that segment failed. If both are missing, leave None — the renderer
    # will tell us during Phase 3.
    def _audio(key: str) -> str | None:
        return mastered.get(key) or raw_audio.get(key)

    def _image(stem: str) -> str | None:
        if not images_dir:
            return None
        # The image engine writes <stem>.png inside ai_images_dir. If the engine
        # falls back to CSS, no PNG exists — we still set the planned path so
        # the renderer can detect and handle the miss explicitly.
        candidate = Path(images_dir) / f"{stem}.png"
        return str(candidate)

    stats = {"intro": 0, "outro": 0, "ayah_scenes": 0, "missing_audio": 0}

    # Intro
    if getattr(episode, "intro_scene", None) is not None:
        path = _audio(_INTRO_KEY)
        episode.intro_scene.audio_path = path
        episode.intro_scene.image_path = _image(_INTRO_KEY)
        if path is None:
            stats["missing_audio"] += 1
            logger.warning("⚠️ propagate: intro audio missing from asset_paths")
        else:
            stats["intro"] = 1

    # Outro
    if getattr(episode, "outro_scene", None) is not None:
        path = _audio(_OUTRO_KEY)
        episode.outro_scene.audio_path = path
        episode.outro_scene.image_path = _image(_OUTRO_KEY)
        if path is None:
            stats["missing_audio"] += 1
            logger.warning("⚠️ propagate: outro audio missing from asset_paths")
        else:
            stats["outro"] = 1

    # Ayah scenes
    for scene in getattr(episode, "ayah_scenes", []) or []:
        key = _scene_key(scene.scene_id)

        scene.intro_audio   = _audio(f"{key}_explain")  # explain doubles as scene intro
        scene.hook_audio    = _audio(f"{key}_hook")
        scene.story_audio   = _audio(f"{key}_story")
        scene.explain_audio = _audio(f"{key}_explain")
        scene.moral_audio   = _audio(f"{key}_moral")
        scene.ayah_audio    = _audio(f"{key}_ayah")
        scene.image_path    = _image(key)

        missing = sum(
            1
            for v in (
                scene.hook_audio,
                scene.story_audio,
                scene.explain_audio,
                scene.moral_audio,
                scene.ayah_audio,
            )
            if v is None
        )
        if missing:
            stats["missing_audio"] += missing
            logger.warning(
                f"⚠️ propagate: scene {scene.scene_id} missing {missing} audio segment(s)"
            )
        stats["ayah_scenes"] += 1

    logger.info(
        f"📝 propagate_paths_to_scenes: intro={stats['intro']}, "
        f"outro={stats['outro']}, ayahs={stats['ayah_scenes']}, "
        f"missing_audio_fields={stats['missing_audio']}"
    )
    return stats


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 → rewrite absolute paths to point at the new runner's filesystem
# ──────────────────────────────────────────────────────────────────────────


def rehydrate_paths_for_new_runner(
    asset_paths: dict[str, Any],
    episode: Any,
    new_ep_dir: str | Path,
) -> int:
    """Rewrite every absolute path from the old runner's temp dir to the new one.

    Call this in Phase 3 AFTER AssetStorage has downloaded files into `new_ep_dir`,
    but BEFORE the renderer consumes asset_paths / episode.scenes.

    Mutates both `asset_paths` and `episode` in place. Returns count of rewrites.
    """
    new_ep_dir = str(Path(new_ep_dir).resolve())
    old_ep_dir = asset_paths.get("ep_dir")
    if not old_ep_dir:
        logger.warning("⚠️ rehydrate: asset_paths.ep_dir missing; cannot rewrite")
        return 0
    old_ep_dir = str(old_ep_dir).rstrip("/")
    if old_ep_dir == new_ep_dir:
        logger.info(f"♻️ rehydrate: ep_dir unchanged ({new_ep_dir}); no rewrites needed")
        return 0

    logger.info(f"🔁 rehydrate: rewriting paths {old_ep_dir} → {new_ep_dir}")

    rewrites = 0

    def _rewrite(value: Any) -> Any:
        nonlocal rewrites
        if isinstance(value, str) and value.startswith(old_ep_dir):
            rewrites += 1
            return new_ep_dir + value[len(old_ep_dir):]
        return value

    # asset_paths.audio_map / mastered_map
    for map_key in ("audio_map", "mastered_map"):
        m = asset_paths.get(map_key)
        if isinstance(m, dict):
            for k, v in list(m.items()):
                m[k] = _rewrite(v)

    # asset_paths.ai_images_dir
    if "ai_images_dir" in asset_paths:
        asset_paths["ai_images_dir"] = _rewrite(asset_paths["ai_images_dir"])

    # Scene-level fields
    scene_audio_fields = (
        "intro_audio", "hook_audio", "story_audio",
        "explain_audio", "moral_audio", "ayah_audio",
        "audio_path",
    )
    scene_image_fields = ("image_path",)

    if getattr(episode, "intro_scene", None) is not None:
        for f in scene_audio_fields + scene_image_fields:
            if hasattr(episode.intro_scene, f):
                setattr(episode.intro_scene, f, _rewrite(getattr(episode.intro_scene, f)))

    if getattr(episode, "outro_scene", None) is not None:
        for f in scene_audio_fields + scene_image_fields:
            if hasattr(episode.outro_scene, f):
                setattr(episode.outro_scene, f, _rewrite(getattr(episode.outro_scene, f)))

    for scene in getattr(episode, "ayah_scenes", []) or []:
        for f in scene_audio_fields + scene_image_fields:
            if hasattr(scene, f):
                setattr(scene, f, _rewrite(getattr(scene, f)))

    # Finally, update ep_dir itself
    asset_paths["ep_dir"] = new_ep_dir

    logger.info(f"🔁 rehydrate: rewrote {rewrites} path(s)")
    return rewrites


# ──────────────────────────────────────────────────────────────────────────
# Sanity check — run this just before the renderer to fail fast & clear
# ──────────────────────────────────────────────────────────────────────────


def verify_render_inputs(episode: Any) -> list[str]:
    """Inspect every scene's audio/image paths and return a list of missing files.

    Call this at the very start of Phase 3's render step. If the returned list
    is non-empty, raise an error with the full list instead of letting the
    renderer die one file at a time.
    """
    missing: list[str] = []

    def _check(scene_label: str, field: str, value: str | None) -> None:
        if not value:
            missing.append(f"{scene_label}.{field} = None")
            return
        if not Path(value).is_file():
            missing.append(f"{scene_label}.{field} → file not found: {value}")

    audio_fields = (
        "intro_audio", "hook_audio", "story_audio",
        "explain_audio", "moral_audio", "ayah_audio",
        "audio_path",
    )

    if getattr(episode, "intro_scene", None) is not None:
        _check("intro_scene", "audio_path", getattr(episode.intro_scene, "audio_path", None))

    for scene in getattr(episode, "ayah_scenes", []) or []:
        label = f"ayah_scene[{scene.scene_id}]"
        for f in audio_fields:
            if hasattr(scene, f):
                v = getattr(scene, f)
                if v is not None:  # only check fields that should be populated
                    _check(label, f, v)

    if getattr(episode, "outro_scene", None) is not None:
        _check("outro_scene", "audio_path", getattr(episode.outro_scene, "audio_path", None))

    return missing
