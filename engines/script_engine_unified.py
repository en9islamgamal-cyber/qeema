"""
engines/script_engine_unified.py — VALUE / QEEMA v22.5
=========================================================================
Strategy-aware script generator that delegates between:
  • Multi-task path (1 Gemini call) — uses script_engine_v20 helpers
  • Legacy path (6 Gemini calls) — uses existing ScriptEngine

[Why this exists]
v20 added script_engine_v20.py with multi-task helpers but never wired
them into the pipeline. v21 added a strategy flag for multi-task but
no code queried it. This module finally MAKES the multi-task path real.

[Design decisions]
1. Wrapper pattern, not modification — ScriptEngine class is untouched
   This means all existing caching/retries/validation still work for the
   legacy path.

2. Multi-task is OPT-IN via strategy. Default behavior (no strategy) is
   identical to the legacy ScriptEngine.

3. Three failure modes for multi-task:
   - LLM call fails → retry up to 2 times
   - Parse fails (LLM returned malformed JSON) → fall back to legacy
   - Schema validation fails (missing fields) → fall back to legacy

4. Cost savings:
   - Legacy: 1 meta call + N ayah calls = 6 calls for 5-ayah episode
   - Multi-task: 1 call total = 83% reduction

5. Quality: Multi-task lets the LLM see ALL ayahs at once, producing
   more coherent narrative cohesion (later ayahs can reference earlier
   ones meaningfully).

[Public API]
    engine = UnifiedScriptEngine(legacy_engine=script_engine, ...)
    script = engine.generate(episode_number, strategy=strategy)

The returned EpisodeScript has the same shape regardless of path taken.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from core.models import (
    EpisodeScript, AyahScene, NarratorScene, VerifiedAyah,
    SceneType, VisualScene, PaletteName, AudioMood, SceneEmotion,
    TransitionType,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# Multi-task path support
# ════════════════════════════════════════════════════════════════
try:
    from engines.script_engine_v20 import (
        build_full_episode_prompt,
        parse_full_episode_response,
    )
    _HAS_MULTI_TASK_HELPERS = True
except ImportError:
    _HAS_MULTI_TASK_HELPERS = False
    build_full_episode_prompt = None  # type: ignore
    parse_full_episode_response = None  # type: ignore


# ════════════════════════════════════════════════════════════════
# UnifiedScriptEngine
# ════════════════════════════════════════════════════════════════
class UnifiedScriptEngine:
    """
    Drops in for ScriptEngine but adds a strategy-driven multi-task path.

    For full backward compat, call .generate(N) without a strategy arg
    and behavior is identical to the legacy engine.
    """

    def __init__(
        self,
        legacy_engine: Any,                # ScriptEngine instance
        *,
        max_multi_task_attempts: int = 2,
        hook_optimizer: Any = None,        # v22.2: Thompson Sampling
    ) -> None:
        self._legacy = legacy_engine
        self._max_attempts = max_multi_task_attempts
        self._hook_optimizer = hook_optimizer

    def _get_first_adapter(self) -> Any:
        """Return the first available Gemini adapter from the script pool.

        Used by TTSDirector and visual_prompt_deep — they need ANY working
        Gemini adapter to make their secondary calls. We pick the first one
        registered in the script_engine's pool (preserves resilience: if
        gemini-1 is broken, gemini-2 is next, etc.).

        Returns None if no adapter found (allows graceful skip).
        """
        try:
            adapters = getattr(self._legacy, "_adapters", None)
            if not adapters:
                return None
            # Prefer gemini adapters over groq for these structured tasks
            for name in ("gemini-1", "gemini-2", "gemini-3"):
                if name in adapters:
                    return adapters[name]
            # Fallback: any adapter
            return next(iter(adapters.values()), None)
        except Exception:
            return None

    # ─── Backward-compatible entry point ────────────────────────
    def generate(
        self,
        episode_number: int,
        *,
        strategy: Any = None,              # PipelineStrategy or None
    ) -> EpisodeScript:
        """Generate a script. If strategy.use_multi_task_script is True
        AND multi-task helpers are available, try the fast path first.

        Falls back to legacy 6-call path on any error.
        """
        # Fast path eligibility check
        try_multi = (
            strategy is not None
            and getattr(strategy, "use_multi_task_script", False)
            and _HAS_MULTI_TASK_HELPERS
            and hasattr(self._legacy, "_call_llm_with_failover")
        )

        if not try_multi:
            logger.info("📝 Using legacy script generation (6 LLM calls)")
            return self._legacy.generate(episode_number)

        # Try multi-task; fall back on any failure
        try:
            return self._generate_multi_task(episode_number)
        except Exception as e:
            logger.warning(
                f"⚠️ Multi-task script failed ({type(e).__name__}: {e}) "
                f"— falling back to legacy 6-call path"
            )
            return self._legacy.generate(episode_number)

    # ─── Multi-task path ────────────────────────────────────────
    def _generate_multi_task(self, episode_number: int) -> EpisodeScript:
        """Single-call multi-task generation using script_engine_v20 helpers.

        Steps:
          1. Look up surah info from curriculum
          2. Check disk cache (legacy engine may have cached this episode)
          3. Fetch verified ayahs from quran.com
          4. Build single multi-task prompt
          5. Call LLM (with retries)
          6. Parse JSON response
          7. Build EpisodeScript with proper field types

        Returns:
          EpisodeScript with same shape as legacy generate()

        Raises:
          On any failure — caller falls back to legacy path.
        """
        from data.curriculum import get_episode_info
        from infrastructure.quran_text_api import fetch_verified_ayahs

        info = get_episode_info(episode_number)

        # Use legacy engine's disk cache if available
        if hasattr(self._legacy, "load_from_disk"):
            cached = self._legacy.load_from_disk(episode_number)
            if cached:
                logger.info(
                    f"♻️ Episode {episode_number}: cached script loaded "
                    f"(via multi-task entry)"
                )
                return cached

        logger.info(
            f"🚀 Multi-task script generation for episode {episode_number} "
            f"(Surah {info['name']}, ayahs {info['start']}-{info['end']})"
        )
        t0 = time.monotonic()

        # 1. Fetch verified ayahs (REQUIRED — never AI-generate Quran text)
        ayahs = fetch_verified_ayahs(info["surah"], info["start"], info["end"])
        t_fetch = time.monotonic() - t0

        # 2. Build multi-task prompt
        ayah_dicts = [
            {"number": a.number, "text": a.text}
            for a in ayahs
        ]
        prompt = build_full_episode_prompt(
            surah_name=info["name"],
            surah_number=info["surah"],
            ayahs=ayah_dicts,
            hook_strategy_hint=self._pick_hook_hint_smart(episode_number),
            analogy_domain_hint=self._pick_analogy_domain(episode_number),
        )

        # 3. Call LLM with retries
        response_text: Optional[str] = None
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response_text = self._call_llm(prompt)
                if response_text:
                    break
            except Exception as e:
                last_error = e
                logger.warning(
                    f"⚠️ Multi-task LLM attempt {attempt} failed: {e}"
                )

        if not response_text:
            raise RuntimeError(
                f"Multi-task LLM exhausted retries: {last_error}"
            )

        t_llm = time.monotonic() - t0 - t_fetch

        # 4. Parse JSON
        data = self._parse_json_response(response_text)

        # 5. Validate schema
        validated = parse_full_episode_response(data, ayah_dicts)

        # 5b. v22.1: Run quality polish (deterministic post-checks + fixes)
        try:
            from engines.script_polisher import polish_script
            polish_report = polish_script(validated, apply_fixes=True)
            if polish_report.has_issues:
                logger.warning(
                    f"⚠️ Multi-task script has quality issues: "
                    f"{len(polish_report.banned_phrases)} banned, "
                    f"{len(polish_report.long_sentences)} long sentences, "
                    f"Egyptian score: {polish_report.egyptian_score:.0%}"
                )
        except ImportError:
            pass  # Polish optional

        # 5c. v22.2: Age-appropriateness check (kids 6-12)
        try:
            from engines.age_appropriateness import (
                check_age_appropriateness, Severity,
            )
            age_report = check_age_appropriateness(validated, log_report=False)
            if age_report.has_critical:
                critical_count = len(age_report.by_severity(Severity.CRITICAL))
                logger.error(
                    f"🚨 Script has CRITICAL age-appropriateness issues "
                    f"({critical_count}): see logs for details"
                )
                logger.error(age_report.summary())
            elif age_report.has_issues:
                logger.warning(age_report.summary())
            else:
                logger.info(age_report.summary())
        except ImportError:
            pass  # Age check optional

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # v22.5 ARCHITECTURE NOTE — Removed from Phase 1:
        #
        # Previously, this section ran:
        #   5d. TTS Director (1 Gemini call for SSML directions)
        #   5e. Deep visual prompts (21 Gemini calls — 3 layers × 7 scenes)
        #
        # These are now executed in Phase 2 (Day 2) using key #2.
        # Phase 1 (Day 1) is dedicated to: tafsir + script + tafsir validation
        # (~14 Gemini calls on key #1 only).
        #
        # The Phase 2 orchestrator block calls these engines on the saved
        # episode JSON before generating Leonardo images and ElevenLabs audio.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # 6. Build EpisodeScript
        script = self._build_episode_script(
            episode_number=episode_number,
            info=info,
            ayahs=ayahs,
            data=validated,
        )

        # 7. Save to disk cache (legacy engine handles this)
        if hasattr(self._legacy, "save_to_disk"):
            try:
                self._legacy.save_to_disk(script, episode_number)
            except Exception as e:
                logger.warning(f"⚠️ Failed to cache script: {e}")

        # 8. Validate quality (reuse legacy engine's validator if present)
        validator = getattr(self._legacy, "_quality_validator", None)
        if validator is not None:
            try:
                ok, notes = validator.validate(script.model_dump())
                if not ok:
                    logger.warning(
                        f"⚠️ Multi-task script failed quality gate: {notes}"
                    )
                    # Don't fail — log and proceed (legacy engine has same behavior)
            except Exception as e:
                logger.warning(f"⚠️ Quality validation skipped: {e}")

        total = time.monotonic() - t0
        logger.info(
            f"✅ Multi-task script done in {total:.1f}s "
            f"(fetch={t_fetch:.1f}s, llm={t_llm:.1f}s)"
        )
        return script

    # ─── LLM call (delegates to legacy engine's failover machinery) ──
    def _call_llm(self, prompt: str) -> str:
        """Call LLM through legacy engine's provider pool (uses circuit breakers)."""
        # The legacy engine has a method like _call_llm_with_failover or similar.
        # Try common names; raise if none found.
        for method_name in [
            "_call_llm_with_failover",
            "_call_llm",
            "_invoke_llm",
        ]:
            if hasattr(self._legacy, method_name):
                method = getattr(self._legacy, method_name)
                # Most signatures are (prompt: str, ...) → str
                try:
                    return method(prompt)
                except TypeError:
                    # Try with operation_name kwarg
                    try:
                        return method(prompt, operation_name="multi_task_script")
                    except Exception:
                        continue
                except Exception:
                    raise

        # Last resort: use the provider pool directly
        pool = getattr(self._legacy, "_provider_pool", None)
        if pool is not None and hasattr(pool, "execute"):
            return pool.execute(
                lambda provider: provider.generate(prompt),
                operation_name="multi_task_script",
            )

        raise RuntimeError(
            "Cannot find LLM call method on legacy ScriptEngine — "
            "multi-task path unsupported"
        )

    @staticmethod
    def _parse_json_response(text: str) -> Dict[str, Any]:
        """Parse LLM JSON response, handling markdown code fences."""
        # Strip code fences if present
        text = re.sub(r'^\s*```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text)
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # Try to find a JSON object in the text
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            raise ValueError(
                f"Could not parse JSON from LLM response: {e}\n"
                f"First 300 chars: {text[:300]}"
            )

    # ─── EpisodeScript builder ──────────────────────────────────
    def _build_episode_script(
        self,
        *,
        episode_number: int,
        info: Dict[str, Any],
        ayahs: List[VerifiedAyah],
        data: Dict[str, Any],
    ) -> EpisodeScript:
        """Convert multi-task JSON response → EpisodeScript pydantic model."""
        # Build intro scene
        intro_scene = NarratorScene(
            scene_id=0,
            scene_type=SceneType.INTRO,
            narrator_text=data["intro_text"],
            visual_prompt=data.get("intro_visual", ""),
            visual_scene=VisualScene.GOLDEN_FIELD,  # safe default
            palette=PaletteName.GOLDEN_HOUR,
            mood=AudioMood.CALM,
            keywords=[],
        )

        # Build outro scene
        outro_scene = NarratorScene(
            scene_id=999,
            scene_type=SceneType.OUTRO,
            narrator_text=data["outro_text"],
            visual_prompt=data.get("outro_visual", ""),
            visual_scene=VisualScene.STARRY_NIGHT,
            palette=PaletteName.NIGHT_STARS,
            mood=AudioMood.PEACEFUL,
            keywords=[],
        )

        # Build ayah scenes
        ayah_scenes: List[AyahScene] = []
        scene_data_list = data["ayah_scenes"]
        # v22.5: pull deep visual results (may be missing/empty if generator failed)
        deep_visuals_list = data.get("_deep_visuals", []) or []

        for i, (ayah, scene_data) in enumerate(zip(ayahs, scene_data_list)):
            # Map LLM emotion string to enum
            emotion_str = scene_data.get("scene_emotion", "warm").lower()
            try:
                emotion = SceneEmotion(emotion_str)
            except ValueError:
                emotion = SceneEmotion.WARM

            # Map visual scene hint
            scene_hint = scene_data.get("visual_scene_hint", "").lower()
            visual_scene = self._map_visual_scene(scene_hint)
            palette = self._map_palette(emotion_str)

            # v22.5: If we have a deep visual result for this scene, use it
            # to build a much richer Leonardo prompt
            deep_visual = (
                deep_visuals_list[i]
                if i < len(deep_visuals_list) else None
            )
            visual_prompt = self._build_visual_prompt(
                scene_data, deep_visual=deep_visual, emotion=emotion_str,
            )

            ayah_scene = AyahScene(
                scene_id=i + 1,
                ayah=ayah,
                intro_text=scene_data.get("intro_text", ""),
                hook_text=scene_data.get("hook_text", ""),
                story_text=scene_data.get("analogy_text", ""),
                explain_text=scene_data.get("explain_text", ""),
                moral_text=scene_data.get("moral_text", ""),
                narrator_text=scene_data.get("explain_text", ""),
                visual_scene=visual_scene,
                palette=palette,
                visual_prompt=visual_prompt,
                scene_emotion=emotion,
                transition_type=TransitionType.FADE,
                keywords=[],
                mood=AudioMood.CALM,
            )
            ayah_scenes.append(ayah_scene)

        # Build full EpisodeScript
        script = EpisodeScript(
            episode_number=episode_number,
            surah=info["surah"],
            surah_name=info["name"],
            title=data["title"],
            youtube_title=data["youtube_title"],
            youtube_description=data["youtube_description"],
            youtube_tags=data.get("youtube_tags", []),
            cta_text=data.get("cta_text", ""),
            intro_scene=intro_scene,
            outro_scene=outro_scene,
            ayah_scenes=ayah_scenes,
            mid_scenes=[],
        )

        return script

    @staticmethod
    def _map_visual_scene(hint: str) -> VisualScene:
        """Map free-text visual hint to VisualScene enum."""
        hint = hint.lower().strip()
        mapping = {
            "golden_field": VisualScene.GOLDEN_FIELD,
            "garden": VisualScene.GARDEN,
            "sky": VisualScene.SKY,
            "mosque": VisualScene.MOSQUE,
            "ocean": VisualScene.OCEAN,
            "starry_night": VisualScene.STARRY_NIGHT,
            "abstract": VisualScene.ABSTRACT_WARM,
            "abstract_warm": VisualScene.ABSTRACT_WARM,
            "abstract_cool": VisualScene.ABSTRACT_COOL,
        }
        return mapping.get(hint, VisualScene.ABSTRACT_WARM)

    @staticmethod
    def _map_palette(emotion: str) -> PaletteName:
        """Map emotion string to palette."""
        e = emotion.lower().strip()
        if e == "reverent":
            return PaletteName.GOLDEN_HOUR
        if e == "playful":
            return PaletteName.WARM_SUNSET
        if e == "peaceful":
            return PaletteName.NIGHT_STARS
        if e == "excited":
            return PaletteName.WARM_SUNSET
        return PaletteName.GOLDEN_HOUR

    @staticmethod
    def _build_visual_prompt(
        scene_data: Dict[str, Any],
        deep_visual: Optional[Dict[str, Any]] = None,
        emotion: str = "warm",
    ) -> str:
        """Build Leonardo positive prompt for a single scene.

        v22.5: If deep_visual data is present (from chained Gemini calls),
        use VisualPromptEngineer.build_from_deep_result() for a much richer
        ~150 token prompt. Otherwise falls back to the original 3-field merge.
        """
        # v22.5: Try deep prompt path
        if deep_visual and deep_visual.get("is_usable"):
            try:
                # Reconstitute a lightweight result-like object
                class _DeepProxy:
                    pass
                proxy = _DeepProxy()
                for k, v in deep_visual.items():
                    setattr(proxy, k, v)
                # is_usable is a property on the real class — set it explicitly
                proxy.is_usable = bool(deep_visual.get("is_usable", False))
                # merge_to_leonardo_prompt method
                from engines.visual_prompt_deep import DeepVisualPromptResult
                # Build a real DeepVisualPromptResult from dict
                real = DeepVisualPromptResult(
                    subject=deep_visual.get("subject", ""),
                    action=deep_visual.get("action", ""),
                    environment=deep_visual.get("environment", ""),
                    time_of_day=deep_visual.get("time_of_day", ""),
                    mood=deep_visual.get("mood", ""),
                    color_palette=deep_visual.get("color_palette", ""),
                    lighting_direction=deep_visual.get("lighting_direction", ""),
                    atmospheric_elements=deep_visual.get("atmospheric_elements", ""),
                    camera_angle=deep_visual.get("camera_angle", ""),
                    depth_of_field=deep_visual.get("depth_of_field", ""),
                    foreground=deep_visual.get("foreground", ""),
                    midground=deep_visual.get("midground", ""),
                    background=deep_visual.get("background", ""),
                    focal_point=deep_visual.get("focal_point", ""),
                    layers_completed=deep_visual.get("layers_completed", 0),
                )
                from engines.visual_prompt_engineer import VisualPromptEngineer
                positive, _ = VisualPromptEngineer.build_from_deep_result(
                    real, emotion=emotion,
                )
                return positive
            except Exception as e:
                logger.warning(
                    f"⚠️ Deep visual merge failed, falling back to shallow: {e}"
                )
                # Fall through to legacy path

        # Legacy path: simple 3-field merge
        subject = scene_data.get("visual_subject", "")
        action = scene_data.get("visual_action", "")
        environment = scene_data.get("visual_environment", "")
        parts = [p for p in [subject, action, environment] if p]
        return ", ".join(parts) if parts else "abstract symbolic scene"

    @staticmethod
    def _pick_hook_hint(episode_number: int) -> str:
        """Vary hook strategies across episodes for diversity."""
        strategies = [
            "amazing scientific fact",
            "common misconception",
            "vivid metaphor from nature",
            "rhetorical question that reframes",
            "contradiction that demands resolution",
            "personal everyday experience",
        ]
        return strategies[episode_number % len(strategies)]

    def _pick_hook_hint_smart(self, episode_number: int) -> str:
        """v22.2: Use HookOptimizer if available, else round-robin.

        HookOptimizer uses Thompson Sampling to pick strategies that
        showed best YouTube retention historically.
        """
        if self._hook_optimizer is not None:
            try:
                strategy = self._hook_optimizer.select_strategy(
                    episode_number, scene_index=0,
                )
                logger.info(f"🎯 HookOptimizer selected: {strategy}")
                return strategy
            except Exception as e:
                logger.warning(f"⚠️ HookOptimizer failed: {e} — falling back")
        return self._pick_hook_hint(episode_number)

    @staticmethod
    def _pick_analogy_domain(episode_number: int) -> str:
        """Vary analogy domains across episodes."""
        domains = [
            "nature and animals",
            "space and astronomy",
            "human body and biology",
            "plants and seeds",
            "water and ocean",
            "everyday objects",
            "weather and seasons",
        ]
        return domains[episode_number % len(domains)]
