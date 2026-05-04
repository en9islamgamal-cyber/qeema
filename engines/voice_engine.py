"""
engines/voice_engine.py — VALUE / QEEMA v15.0
=================================================================
[Changes v15]
- _setup_tts: passes elevenlabs_speed from AudioConfig to ElevenLabsProvider
- generate_episode_audio: synthesizes cta_text as audio (was silently ignored)
- fetch_quran: uses configurable quran_reciter from AudioConfig (was hard-coded "alafasy")
- master_episode: outputs AAC directly instead of MP3→AAC double conversion
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import shutil
import subprocess as sp
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import APIKeysConfig, AudioConfig, EngineConfig, PathsConfig
from core.exceptions import (
    AudioGenerationError,
    ConfigurationError,
    QuranFetchError,
    TransientError,
)
from core.interfaces import (
    QuranAudioRequest,
    QuranAudioResult,
    TTSProvider,
    TTSRequest,
    TTSResult,
)
from core.models import AyahScene, EpisodeScript
from core.resilience import (
    CircuitBreakerConfig,
    ProviderPool,
)
from infrastructure.audio_utils import (
    get_audio_duration,
    normalize_arabic_for_tts,
    stable_cache_key,
    validate_audio_file,
)
from infrastructure.parallel_quran import ParallelQuranFetcher
from infrastructure.quran_sources import default_sources
from infrastructure.tts_providers import (
    ElevenLabsProvider,
    GoogleTTSProvider,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Audio mastering — v15: outputs AAC (no double-conversion)
# ════════════════════════════════════════════════════════════════
def _master_audio(
    input_path: str,
    output_path: str,
    *,
    is_quran: bool,
    target_lufs: float,
    true_peak: float,
    quran_delay_ms: int,
    quran_fade_in: float,
) -> bool:
    """Apply loudnorm + fades. Outputs AAC for pipeline consistency."""
    if is_quran:
        filters = (
            f"aresample=44100,"
            f"adelay={quran_delay_ms}|{quran_delay_ms},"
            f"afade=t=in:st=0:d={quran_fade_in:.2f},"
            f"apad=pad_dur=1.0,"
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11"
        )
    else:
        filters = (
            "aresample=44100,"
            "afade=t=in:st=0:d=0.2,"
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11"
        )

    # v15: Use AAC codec directly — avoids MP3→AAC re-encode in final assembly
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", filters,
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]
    try:
        result = sp.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except sp.TimeoutExpired:
        logger.warning(f"⚠️ master_audio timeout for {input_path}")
        return False


# ════════════════════════════════════════════════════════════════
# Quran fetcher (CDN pool)
# ════════════════════════════════════════════════════════════════
class _QuranFetcher:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir: Path = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._sources = default_sources()
        self._sources_by_name = {s.name: s for s in self._sources}
        self._pool = ProviderPool("quran_cdn", strategy="fastest")
        for src in self._sources:
            self._pool.register(
                src.name,
                breaker_config=CircuitBreakerConfig(
                    failure_threshold=3,
                    recovery_timeout_sec=120.0,
                ),
            )

    def _cache_path(self, surah: int, ayah: int, reciter: str) -> Path:
        return self._cache_dir / f"{reciter}_{surah:03d}_{ayah:03d}.mp3"

    def fetch(self, request: QuranAudioRequest) -> QuranAudioResult:
        cache_path = self._cache_path(
            request.surah, request.ayah, request.reciter,
        )
        if cache_path.exists() and validate_audio_file(str(cache_path)):
            shutil.copy(cache_path, request.output_path)
            logger.info(f"♻️ Quran cache hit: {request.surah}:{request.ayah}")
            return QuranAudioResult(
                output_path=request.output_path,
                duration_sec=get_audio_duration(request.output_path),
                source="cache",
                cached=True,
            )

        tried: List[str] = []

        def _invoke(provider_name: str) -> QuranAudioResult:
            src = self._sources_by_name[provider_name]
            tried.append(provider_name)
            return src.fetch(request)

        try:
            result = self._pool.execute(_invoke)
        except Exception as e:
            raise QuranFetchError(
                request.surah, request.ayah,
                sources_tried=tried,
                cause=e,
            ) from e

        try:
            shutil.copy(request.output_path, cache_path)
        except OSError as e:
            logger.warning(f"⚠️ Quran cache write failed: {e}")

        return result

    def health_report(self) -> dict:
        return self._pool.health_report()


# ════════════════════════════════════════════════════════════════
# VoiceEngine
# ════════════════════════════════════════════════════════════════
class VoiceEngine:
    """Production voice engine: TTS + Quran + cache + parallel synthesis."""

    def __init__(
        self,
        *,
        api_keys: APIKeysConfig,
        paths: PathsConfig,
        audio_cfg: AudioConfig,
        engine_cfg: EngineConfig,
    ) -> None:
        self._paths: PathsConfig = paths
        self._audio_cfg: AudioConfig = audio_cfg
        self._engine_cfg: EngineConfig = engine_cfg

        self._providers: Dict[str, TTSProvider] = {}
        self._tts_pool: ProviderPool = ProviderPool("tts", strategy="round_robin")
        self._setup_tts(api_keys)

        self._reciter: _QuranFetcher = _QuranFetcher(paths.quran_cache)
        self._parallel_quran: ParallelQuranFetcher = ParallelQuranFetcher(
            fetch_fn=self._reciter.fetch,
            max_workers=getattr(engine_cfg, "quran_parallel_workers", 6),
            per_request_timeout_sec=60.0,
            fail_fast=False,
        )

        paths.tts_cache.mkdir(parents=True, exist_ok=True)

    def _setup_tts(self, api_keys: APIKeysConfig) -> None:
        if api_keys.elevenlabs:
            try:
                p = ElevenLabsProvider(
                    api_keys.elevenlabs,
                    api_keys.elevenlabs_voice_id,
                    model=self._audio_cfg.elevenlabs_model,
                    stability=self._audio_cfg.elevenlabs_stability,
                    similarity=self._audio_cfg.elevenlabs_similarity,
                    style=self._audio_cfg.elevenlabs_style,
                    speaker_boost=self._audio_cfg.elevenlabs_speaker_boost,
                    speed=getattr(self._audio_cfg, "elevenlabs_speed", 0.85),  # v15
                )
                self._providers[p.name] = p
                self._tts_pool.register(
                    p.name,
                    breaker_config=CircuitBreakerConfig(
                        failure_threshold=4, recovery_timeout_sec=60.0,
                    ),
                    rate_limit=(1.5, 5),
                )
            except Exception as e:
                logger.warning(f"⚠️ ElevenLabs init failed: {e}")

        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            try:
                p = GoogleTTSProvider(
                    voice=self._audio_cfg.google_voice,
                    speaking_rate=self._audio_cfg.google_speaking_rate,
                    pitch=self._audio_cfg.google_pitch,
                )
                self._providers[p.name] = p
                self._tts_pool.register(
                    p.name,
                    breaker_config=CircuitBreakerConfig(
                        failure_threshold=4, recovery_timeout_sec=45.0,
                    ),
                )
            except Exception as e:
                logger.warning(f"⚠️ Google TTS init failed: {e}")

        if not self._providers:
            raise ConfigurationError(
                "No TTS providers available "
                "(set ELEVENLABS_API_KEY or GOOGLE_APPLICATION_CREDENTIALS)"
            )
        logger.info(f"✅ VoiceEngine: TTS providers = {list(self._providers.keys())}")

    # ───────────────────────────────────────────────────────────
    def synthesize(self, text: str, output_path: str) -> TTSResult:
        if not text or not text.strip():
            raise AudioGenerationError("Empty text for TTS")

        normalized = normalize_arabic_for_tts(text)
        primary_voice = self._primary_voice_id()

        # v15.1 fix: include voice settings in cache key.
        # Without this, changing stability/style/speed serves stale
        # audio from before the change.
        voice_signature = (
            f"v15|stab={self._audio_cfg.elevenlabs_stability:.2f}"
            f"|sim={self._audio_cfg.elevenlabs_similarity:.2f}"
            f"|sty={self._audio_cfg.elevenlabs_style:.2f}"
            f"|spd={self._audio_cfg.elevenlabs_speed:.2f}"
            f"|model={self._audio_cfg.elevenlabs_model}"
        )
        cache_path = (
            self._paths.tts_cache
            / f"{stable_cache_key(primary_voice, voice_signature, normalized)}.mp3"
        )

        if (
            self._engine_cfg.voice_enable_cache
            and cache_path.exists()
            and validate_audio_file(str(cache_path))
        ):
            shutil.copy(cache_path, output_path)
            return TTSResult(
                output_path=output_path,
                duration_sec=get_audio_duration(output_path),
                provider="cache",
                voice_id=primary_voice,
                cached=True,
            )

        request = TTSRequest(text=normalized, output_path=output_path)

        def _invoke(provider_name: str) -> TTSResult:
            return self._providers[provider_name].synthesize(request)

        result = self._tts_pool.execute(_invoke)

        if self._engine_cfg.voice_enable_cache:
            try:
                shutil.copy(output_path, cache_path)
            except OSError as e:
                logger.warning(f"⚠️ TTS cache write failed: {e}")

        return result

    def synthesize_batch(
        self,
        items: List[Tuple[str, str]],
    ) -> Dict[str, TTSResult]:
        if not items:
            return {}

        results: Dict[str, TTSResult] = {}
        errors: List[str] = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._engine_cfg.voice_parallel_workers,
            thread_name_prefix="tts",
        ) as executor:
            future_to_item = {
                executor.submit(self.synthesize, text, out): (text, out)
                for text, out in items
            }
            for future in concurrent.futures.as_completed(future_to_item):
                _, out = future_to_item[future]
                try:
                    results[out] = future.result()
                except Exception as e:
                    errors.append(f"  • {out}: {type(e).__name__}: {e}")

        if errors:
            raise AudioGenerationError(
                f"Batch TTS failed for {len(errors)}/{len(items)} items:\n"
                + "\n".join(errors)
            )
        return results

    def fetch_quran(
        self,
        surah: int,
        ayah: int,
        output_path: str,
        *,
        reciter: Optional[str] = None,
    ) -> QuranAudioResult:
        # v15: use configured reciter; fall back to "alafasy"
        r = reciter or getattr(self._audio_cfg, "quran_reciter", "alafasy")
        return self._reciter.fetch(
            QuranAudioRequest(
                surah=surah, ayah=ayah,
                output_path=output_path, reciter=r,
            )
        )

    def master_episode(
        self,
        audio_map: Dict[str, str],
        ep_dir: Path,
    ) -> Dict[str, str]:
        mastered: Dict[str, str] = {}
        out_dir = ep_dir / "mastered"
        out_dir.mkdir(parents=True, exist_ok=True)

        for key, src_path in audio_map.items():
            if not Path(src_path).exists():
                continue
            # v15: output as .m4a (proper AAC container with duration metadata)
            dst = out_dir / (Path(src_path).stem + ".m4a")
            is_quran = ("ayah" in key) or ("recite" in key)
            ok = _master_audio(
                src_path,
                str(dst),
                is_quran=is_quran,
                target_lufs=self._audio_cfg.target_lufs,
                true_peak=self._audio_cfg.true_peak,
                quran_delay_ms=self._audio_cfg.quran_start_delay_ms,
                quran_fade_in=self._audio_cfg.quran_fade_in_sec,
            )
            mastered[key] = str(dst) if ok else src_path
        return mastered

    def generate_episode_audio(
        self,
        script: EpisodeScript,
        ep_dir: Path,
    ) -> Dict[str, str]:
        """
        Generate all TTS audio for an episode.
        v15: also synthesizes cta_text (was silently ignored before).
        Returns map: stage_key → file_path.
        """
        ep_dir.mkdir(parents=True, exist_ok=True)
        audio_map: Dict[str, str] = {}

        tts_items: List[Tuple[str, str]] = []

        # ── Intro narrator
        if script.intro_scene.narrator_text:
            p = str(ep_dir / "intro_narrator.mp3")
            tts_items.append((script.intro_scene.narrator_text, p))

        # v15 NEW: CTA text (subscribe call-to-action)
        cta = getattr(script, "cta_text", None)
        if cta:
            tts_items.append((cta, str(ep_dir / "intro_cta.mp3")))

        # ── Ayah narration segments
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            if scene.intro_text:
                tts_items.append(
                    (scene.intro_text, str(ep_dir / f"{sid}_intro.mp3"))
                )
            if scene.explain_text:
                tts_items.append(
                    (scene.explain_text, str(ep_dir / f"{sid}_explain.mp3"))
                )

        # ── Mid-scenes
        for sc in script.mid_scenes:
            tts_items.append(
                (sc.narrator_text, str(ep_dir / f"mid_{sc.scene_id}.mp3"))
            )

        # ── Outro narrator
        if script.outro_scene.narrator_text:
            tts_items.append(
                (script.outro_scene.narrator_text, str(ep_dir / "outro_narrator.mp3"))
            )

        logger.info(f"🎙️ Synthesizing {len(tts_items)} TTS items in parallel")
        self.synthesize_batch(tts_items)

        # ── Map TTS outputs back to keys
        if script.intro_scene.narrator_text:
            audio_map["intro"] = str(ep_dir / "intro_narrator.mp3")
            script.intro_scene.audio_path = audio_map["intro"]

        # v15 NEW: wire CTA audio
        cta_path = str(ep_dir / "intro_cta.mp3")
        if cta and Path(cta_path).exists():
            audio_map["cta"] = cta_path
            logger.info("🎙️ CTA audio generated")

        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            if scene.intro_text:
                p = str(ep_dir / f"{sid}_intro.mp3")
                audio_map[f"{sid}_intro"] = p
                scene.intro_audio = p
            if scene.explain_text:
                p = str(ep_dir / f"{sid}_explain.mp3")
                audio_map[f"{sid}_explain"] = p
                scene.explain_audio = p

        for sc in script.mid_scenes:
            p = str(ep_dir / f"mid_{sc.scene_id}.mp3")
            audio_map[f"mid_{sc.scene_id}"] = p
            sc.audio_path = p

        if script.outro_scene.narrator_text:
            audio_map["outro"] = str(ep_dir / "outro_narrator.mp3")
            script.outro_scene.audio_path = audio_map["outro"]

        # ── Quran fetches (parallel)
        if script.ayah_scenes:
            quran_requests: List[QuranAudioRequest] = []
            request_to_scene: Dict[str, Tuple[str, AyahScene]] = {}
            reciter = getattr(self._audio_cfg, "quran_reciter", "alafasy")

            for scene in script.ayah_scenes:
                sid = f"ayah_{scene.scene_id}"
                output_path = str(ep_dir / f"{sid}_recitation.mp3")
                req = QuranAudioRequest(
                    surah=scene.ayah.surah,
                    ayah=scene.ayah.number,
                    output_path=output_path,
                    reciter=reciter,   # v15: configurable
                )
                quran_requests.append(req)
                request_to_scene[output_path] = (sid, scene)

            logger.info(f"📥 Fetching {len(quran_requests)} Quran ayahs in parallel")
            batch = self._parallel_quran.fetch_batch(quran_requests)

            for path in batch.successes:
                sid, scene = request_to_scene[path]
                audio_map[f"{sid}_ayah"] = path
                scene.ayah_audio = path

            if batch.failures:
                first_failure_path = next(iter(batch.failures))
                first_failure_exc = batch.failures[first_failure_path]
                _, failed_scene = request_to_scene[first_failure_path]
                failure_summary = "; ".join(
                    f"{Path(p).name}: {type(e).__name__}"
                    for p, e in list(batch.failures.items())[:5]
                )
                raise QuranFetchError(
                    surah=failed_scene.ayah.surah,
                    ayah=failed_scene.ayah.number,
                    sources_tried=[],
                    cause=Exception(
                        f"{len(batch.failures)} ayah(s) failed: {failure_summary}"
                    ),
                ) from first_failure_exc

        logger.info(f"✅ Episode audio: {len(audio_map)} files generated")
        return audio_map

    def _primary_voice_id(self) -> str:
        for p in self._providers.values():
            if p.voice_id:
                return p.voice_id
        return "unknown"

    def health_report(self) -> dict:
        return {
            "tts": self._tts_pool.health_report(),
            "quran": self._reciter.health_report(),
        }
