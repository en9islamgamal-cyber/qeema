"""Tests for core.pipeline_strategy."""
import pytest

from core.pipeline_strategy import (
    StrategyFactory,
    PipelineStrategy,
    QualityMode,
    parse_mode,
)


class MockQuotaManager:
    def __init__(self, leo: int, el: int, eps_done: int = 0):
        self._leo = leo
        self._el = el
        self._state = type("s", (), {
            "episodes_completed_this_month": eps_done
        })()

    def leonardo_remaining(self) -> int:
        return self._leo

    def elevenlabs_remaining(self) -> int:
        return self._el


# ════════════════════════════════════════════════════════════════
# parse_mode
# ════════════════════════════════════════════════════════════════
class TestParseMode:
    def test_valid_modes(self):
        assert parse_mode("high") == QualityMode.HIGH
        assert parse_mode("balanced") == QualityMode.BALANCED
        assert parse_mode("economy") == QualityMode.ECONOMY
        assert parse_mode("auto") == QualityMode.AUTO

    def test_case_insensitive(self):
        assert parse_mode("HIGH") == QualityMode.HIGH
        assert parse_mode("Balanced") == QualityMode.BALANCED

    def test_whitespace_handling(self):
        assert parse_mode("  high  ") == QualityMode.HIGH

    def test_none_defaults_to_auto(self):
        assert parse_mode(None) == QualityMode.AUTO
        assert parse_mode("") == QualityMode.AUTO

    def test_invalid_falls_back_to_auto(self):
        assert parse_mode("invalid") == QualityMode.AUTO
        assert parse_mode("super-high") == QualityMode.AUTO


# ════════════════════════════════════════════════════════════════
# StrategyFactory auto-selection
# ════════════════════════════════════════════════════════════════
class TestAutoSelection:
    def test_high_when_quota_ample(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.AUTO,
            quota_manager=MockQuotaManager(leo=150, el=30000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.HIGH
        assert s.max_ai_images == 7
        assert s.use_claude_tafsir is True
        assert s.use_adaptive_voice is True
        assert "auto-selected HIGH" in s.reasoning

    def test_balanced_when_quota_tight(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.AUTO,
            quota_manager=MockQuotaManager(leo=20, el=3000),
            episode_number=5,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.BALANCED
        assert s.max_ai_images == 5
        assert s.use_claude_tafsir is True
        assert "auto-selected BALANCED" in s.reasoning

    def test_economy_when_quota_critical(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.AUTO,
            quota_manager=MockQuotaManager(leo=5, el=1000),
            episode_number=7,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.ECONOMY
        assert s.max_ai_images == 3
        assert s.use_claude_tafsir is False  # heuristic only
        assert s.use_adaptive_voice is False
        assert "auto-selected ECONOMY" in s.reasoning

    def test_no_quota_manager_defaults_to_high(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.AUTO,
            quota_manager=None,
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.HIGH


# ════════════════════════════════════════════════════════════════
# Mode capping (user request vs reality)
# ════════════════════════════════════════════════════════════════
class TestModeCapping:
    def test_high_capped_to_balanced_when_quota_low(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,  # User wants HIGH
            quota_manager=MockQuotaManager(leo=20, el=3000),  # But quota is BALANCED
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.BALANCED
        assert "capped" in s.reasoning.lower()

    def test_high_capped_to_economy_when_quota_critical(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=5, el=1000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.ECONOMY

    def test_balanced_capped_to_economy(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.BALANCED,
            quota_manager=MockQuotaManager(leo=5, el=1000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.ECONOMY

    def test_economy_always_allowed(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.ECONOMY,
            quota_manager=MockQuotaManager(leo=200, el=30000),  # ample
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.ECONOMY

    def test_high_honored_when_quota_allows(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.mode == QualityMode.HIGH


# ════════════════════════════════════════════════════════════════
# Engine availability degrades strategy
# ════════════════════════════════════════════════════════════════
class TestEngineAvailability:
    def test_no_anthropic_key_disables_claude_tafsir(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=False,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.use_claude_tafsir is False
        assert s.use_batched_tafsir is False

    def test_no_leonardo_engine_zero_images(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=False,
            has_multi_task_engine=True,
        )
        assert s.max_ai_images == 0
        assert s.image_reuse_strategy == "css_only"

    def test_no_multi_task_engine(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=False,
        )
        assert s.use_multi_task_script is False


# ════════════════════════════════════════════════════════════════
# Quality thresholds
# ════════════════════════════════════════════════════════════════
class TestQualityThresholds:
    def test_high_threshold(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.quality_threshold == 70.0

    def test_balanced_threshold(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.BALANCED,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.quality_threshold == 65.0

    def test_economy_threshold(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.ECONOMY,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert s.quality_threshold == 60.0


# ════════════════════════════════════════════════════════════════
# Reports
# ════════════════════════════════════════════════════════════════
class TestStrategyReports:
    def test_summary_contains_mode(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        assert "high" in s.summary().lower()
        assert "PipelineStrategy" in s.summary()

    def test_detailed_report_has_all_fields(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.BALANCED,
            quota_manager=MockQuotaManager(leo=20, el=3000),
            episode_number=5,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        report = s.detailed_report()
        assert "BALANCED" in report
        assert "Multi-task script" in report
        assert "Batched tafsir" in report
        assert "Max AI images" in report

    def test_immutability(self):
        s = StrategyFactory.build(
            requested_mode=QualityMode.HIGH,
            quota_manager=MockQuotaManager(leo=100, el=20000),
            episode_number=1,
            has_anthropic_key=True,
            has_leonardo_engine=True,
            has_multi_task_engine=True,
        )
        # Frozen dataclass should reject mutation
        with pytest.raises((AttributeError, Exception)):
            s.mode = QualityMode.ECONOMY  # type: ignore
