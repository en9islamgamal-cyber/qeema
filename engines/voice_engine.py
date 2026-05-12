"""
engines/voice_engine.py — VALUE / QEEMA v22.5 — voice synthesis coordinator
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
        quota_manager=None,  # v19: optional QuotaManager for budget enforcement
    ) -> None:
        self._paths: PathsConfig = paths
        self._audio_cfg: AudioConfig = audio_cfg
        self._engine_cfg: EngineConfig = engine_cfg
        self._quota_manager = quota_manager  # v19

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

        # v22.5: CAMB.AI as second-tier fallback (after ElevenLabs, before Google).
        # The user has CAMB_AI_KEY in repo secrets — we use it ONLY when
        # ElevenLabs fails or runs out of quota. Egyptian-dialect quality lives
        # in ElevenLabs; CAMB MARS8 is MSA Arabic — acceptable as a fallback,
        # not as primary.
        camb_key = os.getenv("CAMB_AI_KEY", "").strip()
        camb_voice_id = os.getenv("CAMB_AI_VOICE_ID", "").strip()
        if camb_key and camb_voice_id:
            try:
                from infrastructure.tts_providers import CambAIProvider
                # CAMB_AI_LANGUAGE_ID is optional. If unset, the provider
                # auto-discovers Arabic via /source-languages on first call.
                # Set it explicitly if auto-discovery is unreliable for you.
                lang_override_str = os.getenv("CAMB_AI_LANGUAGE_ID", "").strip()
                lang_override = (
                    int(lang_override_str) if lang_override_str.isdigit()
                    else None
                )
                p = CambAIProvider(
                    api_key=camb_key,
                    voice_id=int(camb_voice_id),
                    speech_model=os.getenv("CAMB_AI_SPEECH_MODEL", "mars-pro"),
                    language_id_override=lang_override,
                )
                self._providers[p.name] = p
                self._tts_pool.register(
                    p.name,
                    breaker_config=CircuitBreakerConfig(
                        failure_threshold=3, recovery_timeout_sec=90.0,
                    ),
                    rate_limit=(0.5, 2),  # 30 req/min, conservative
                )
                logger.info("✅ CambAI fallback wired (Arabic MSA via MARS8)")
            except ValueError as e:
                # Bad voice_id integer or missing key — log and skip
                logger.warning(f"⚠️ CambAI init bad params: {e}")
            except Exception as e:
                logger.warning(f"⚠️ CambAI init failed: {e}")
        elif camb_key and not camb_voice_id:
            logger.warning(
                "⚠️ CAMB_AI_KEY is set but CAMB_AI_VOICE_ID is missing — "
                "CambAI fallback NOT wired. Get a voice_id from "
                "https://studio.camb.ai (or call /list-voices) and set the env var."
            )

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
    def synthesize(
        self,
        text: str,
        output_path: str,
        emotion: Optional[str] = None,  # v23: per-segment emotion override
    ) -> TTSResult:
        if not text or not text.strip():
            raise AudioGenerationError("Empty text for TTS")

        normalized = normalize_arabic_for_tts(text)
        primary_voice = self._primary_voice_id()

        # v23: If emotion specified, include in cache key (different settings = different cache)
        emotion_signature = f"|emo={emotion}" if emotion else ""

        # v15.1 fix: include voice settings in cache key.
        voice_signature = (
            f"v15|stab={self._audio_cfg.elevenlabs_stability:.2f}"
            f"|sim={self._audio_cfg.elevenlabs_similarity:.2f}"
            f"|sty={self._audio_cfg.elevenlabs_style:.2f}"
            f"|spd={self._audio_cfg.elevenlabs_speed:.2f}"
            f"|model={self._audio_cfg.elevenlabs_model}"
            f"{emotion_signature}"
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

        # v19: Quota check BEFORE making API call (prevents wasted credits)
        char_estimate = len(normalized)
        if self._quota_manager is not None:
            if not self._quota_manager.can_consume_elevenlabs(char_estimate):
                # v22.5: Fallback chain — CambAI first (better quality than Google TTS),
                # then Google TTS as last resort.
                request = TTSRequest(
                    text=normalized, output_path=output_path, emotion=emotion,
                )
                if "camb_ai" in self._providers:
                    # v22.5: hard cap on CambAI fallback to prevent runaway
                    # spending if ElevenLabs is down for an extended period.
                    if not self._quota_manager.can_consume_camb(char_estimate):
                        camb_remaining = self._quota_manager.camb_remaining()
                        logger.warning(
                            f"⚠️ CambAI monthly cap reached "
                            f"({camb_remaining} chars remaining, need "
                            f"{char_estimate}) — falling through to Google TTS"
                        )
                    else:
                        logger.warning(
                            f"⚠️ ElevenLabs quota low — falling back to CambAI "
                            f"for {char_estimate} chars"
                        )
                        try:
                            result = self._providers["camb_ai"].synthesize(request)
                            # Only record consumption on success
                            self._quota_manager.consume_camb(char_estimate)
                            return result
                        except Exception as e:
                            logger.warning(
                                f"⚠️ CambAI fallback also failed ({e}) — "
                                f"trying Google TTS"
                            )
                            # Fall through to Google TTS
                if "google_tts" in self._providers:
                    logger.warning(
                        f"⚠️ Falling back to Google TTS for {char_estimate} chars"
                    )
                    return self._providers["google_tts"].synthesize(request)
                raise AudioGenerationError(
                    f"ElevenLabs quota exhausted "
                    f"({self._quota_manager.elevenlabs_remaining()} remaining), "
                    f"need {char_estimate}. No fallback configured "
                    f"(set CAMB_AI_KEY+CAMB_AI_VOICE_ID or "
                    f"GOOGLE_APPLICATION_CREDENTIALS)."
                )

        request = TTSRequest(text=normalized, output_path=output_path, emotion=emotion)

        def _invoke(provider_name: str) -> TTSResult:
            return self._providers[provider_name].synthesize(request)

        result = self._tts_pool.execute(_invoke)

        # v22.5: Record actual consumption per-provider so each fallback path
        # is properly accounted for. The pool can failover transparently when
        # ElevenLabs raises an exception (auth, network, rate-limit) — when
        # that happens, CambAI may have answered without us hitting the
        # explicit quota-fallback branch above. Track that here.
        if self._quota_manager is not None:
            if result.provider == "elevenlabs":
                self._quota_manager.consume_elevenlabs(char_estimate)
            elif result.provider == "camb_ai":
                # Pool-level failover used CambAI without going through the
                # quota-aware branch — record consumption now to keep the
                # monthly cap honest.
                self._quota_manager.consume_camb(char_estimate)
            # google_tts has no per-call quota tracking (free tier large enough)

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

    # ════════════════════════════════════════════════════════════════
    # v23: Per-segment emotion-aware batch synthesis
    # ════════════════════════════════════════════════════════════════
    def synthesize_batch_with_emotions(
        self,
        items: List[Tuple[str, str, Optional[str]]],
    ) -> Dict[str, TTSResult]:
        """Like synthesize_batch but each item carries (text, output_path, emotion).

        Each call uses emotion-specific voice settings (stability/style/speed)
        from the TTS provider's EMOTION_VOICE_OVERRIDES.

        Args:
            items: List of (text, output_path, emotion) — emotion can be None
                   for default settings.

        Returns:
            Dict[output_path → TTSResult].

        Raises:
            AudioGenerationError if any item fails.
        """
        if not items:
            return {}

        results: Dict[str, TTSResult] = {}
        errors: List[str] = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._engine_cfg.voice_parallel_workers,
            thread_name_prefix="tts_emo",
        ) as executor:
            future_to_item = {
                executor.submit(self.synthesize, text, out, emo): (text, out, emo)
                for text, out, emo in items
            }
            for future in concurrent.futures.as_completed(future_to_item):
                _, out, _ = future_to_item[future]
                try:
                    results[out] = future.result()
                except Exception as e:
                    errors.append(f"  • {out}: {type(e).__name__}: {e}")

        if errors:
            raise AudioGenerationError(
                f"Emotion-batch TTS failed for {len(errors)}/{len(items)} items:\n"
                + "\n".join(errors)
            )
        return results

    # ════════════════════════════════════════════════════════════════
    # v20: Per-scene combined synthesis (cost optimization)
    # ════════════════════════════════════════════════════════════════
    def synthesize_combined(
        self,
        segments: List[Tuple[str, str]],
        output_path: str,
        *,
        emotion: str = "warm",
        join_pause_ms: int = 600,
    ) -> TTSResult:
        """
        v20: Synthesize multiple text segments as ONE TTS call.

        Joins segments with SSML <break> tags, calls API once, returns
        combined audio. Eliminates per-segment overhead.

        Args:
            segments: List of (text, label) — label is for logging only
            output_path: Where to save combined audio
            emotion: Voice emotion override
            join_pause_ms: Pause between segments (default 600ms)

        Returns:
            Single TTSResult for the combined audio.

        [Cost saving]
            Old: 5 segments × ~80 char overhead per request = 400 wasted chars
            New: 1 request with 5 segments + 4 breaks = minimal overhead

        [Use case]
            Per-ayah scene has hook + intro + analogy + explain + moral.
            Instead of 5 separate calls, synthesize as one and use
            silence detection / segment markers to split for video.
        """
        if not segments:
            raise AudioGenerationError("No segments provided")

        # Filter empty segments
        valid_segments = [(t, lbl) for t, lbl in segments if t and t.strip()]
        if not valid_segments:
            raise AudioGenerationError("All segments empty")

        # Build combined text with SSML breaks
        # eleven_multilingual_v2 supports <break time="Xms"/>
        joiner = f'<break time="{join_pause_ms}ms"/>'
        combined_text = joiner.join(t for t, _ in valid_segments)

        labels = [lbl for _, lbl in valid_segments]
        char_total = sum(len(t) for t, _ in valid_segments)
        logger.info(
            f"🎙️ Combined TTS: {len(valid_segments)} segments → {char_total} chars "
            f"({', '.join(labels)})"
        )

        # Call standard synthesize with the combined text
        # (synthesize handles emotion via TTSRequest.emotion if supported)
        return self.synthesize(combined_text, output_path)

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

        tts_items: List[Tuple[str, str, Optional[str]]] = []

        # ── Intro narrator
        if script.intro_scene.narrator_text:
            p = str(ep_dir / "intro_narrator.mp3")
            tts_items.append((script.intro_scene.narrator_text, p, "intro:excited"))

        # v15 NEW: CTA text (subscribe call-to-action)
        cta = getattr(script, "cta_text", None)
        if cta:
            tts_items.append((cta, str(ep_dir / "intro_cta.mp3"), "cta:warm"))

        # ── Ayah narration segments
        for scene in script.ayah_scenes:
            sid = f"ayah_{scene.scene_id}"
            if scene.intro_text:
                tts_items.append(
                    (scene.intro_text, str(ep_dir / f"{sid}_intro.mp3"), "explain:warm")
                )
            if scene.explain_text:
                tts_items.append(
                    (scene.explain_text, str(ep_dir / f"{sid}_explain.mp3"), "explain:warm")
                )

        # ── Mid-scenes
        for sc in script.mid_scenes:
            tts_items.append(
                (sc.narrator_text, str(ep_dir / f"mid_{sc.scene_id}.mp3"), "story:warm")
            )

        # ── Outro narrator
        if script.outro_scene.narrator_text:
            tts_items.append(
                (script.outro_scene.narrator_text, str(ep_dir / "outro_narrator.mp3"), "outro:warm")
            )

        logger.info(f"🎙️ Synthesizing {len(tts_items)} emotion-aware TTS items in parallel")
        self.synthesize_batch_with_emotions(tts_items)

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
