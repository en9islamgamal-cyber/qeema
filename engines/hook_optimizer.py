"""
engines/hook_optimizer.py — VALUE / QEEMA v18.0 (NEW)
=========================================================================
Data-driven hook strategy selection based on YouTube retention.

[Algorithm: Multi-Armed Bandit + Thompson Sampling]
- Each hook strategy = a "bandit arm"
- Track YouTube retention at 30s + 60s per hook type
- During exploration phase (first 20 episodes): pure round-robin
- After: Thompson Sampling (Beta posterior, balances explore/exploit)
- Update weekly from YouTube Analytics API
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Hook strategies (must match engines/script_engine.py) ──────
HOOK_STRATEGIES: List[str] = [
    "scientific_question",
    "statistic_or_number",
    "contradiction",
    "mental_challenge",
    "daily_observation",
    "nature_fact",
]


@dataclass
class HookPerformance:
    """Stats for a single hook strategy."""
    strategy: str
    trials: int = 0
    cumulative_retention_30s: float = 0.0
    cumulative_retention_60s: float = 0.0
    successes: int = 0  # episodes where retention_30s > SUCCESS_BAR

    @property
    def avg_retention_30s(self) -> float:
        return self.cumulative_retention_30s / self.trials if self.trials else 0.0

    @property
    def avg_retention_60s(self) -> float:
        return self.cumulative_retention_60s / self.trials if self.trials else 0.0


class HookOptimizer:
    """Multi-armed bandit for hook strategy optimization."""

    EXPLORATION_THRESHOLD = 20
    SUCCESS_RETENTION_BAR = 0.50

    def __init__(self, paths) -> None:
        """
        Args:
            paths: PathsConfig object (must have .logs Path attribute)
        """
        self._paths = paths
        self._log_file = Path(paths.logs) / "hook_performance.jsonl"
        self._stats_file = Path(paths.logs) / "hook_stats.json"
        self._stats: Dict[str, HookPerformance] = {}
        self._load_stats()

    def _load_stats(self) -> None:
        for s in HOOK_STRATEGIES:
            self._stats[s] = HookPerformance(strategy=s)
        if not self._log_file.exists():
            return
        try:
            with self._log_file.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        s = e.get("strategy")
                        if s in self._stats:
                            self._stats[s].trials += 1
                            r30 = float(e.get("retention_30s", 0.0))
                            r60 = float(e.get("retention_60s", 0.0))
                            self._stats[s].cumulative_retention_30s += r30
                            self._stats[s].cumulative_retention_60s += r60
                            if r30 >= self.SUCCESS_RETENTION_BAR:
                                self._stats[s].successes += 1
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError as e:
            logger.warning(f"⚠️ Could not load hook stats: {e}")

    def select_strategy(self, episode_number: int, scene_index: int = 0) -> str:
        """Returns the optimal hook strategy for this episode."""
        total_trials = sum(s.trials for s in self._stats.values())

        if total_trials < self.EXPLORATION_THRESHOLD:
            idx = (episode_number * 7 + scene_index * 3) % len(HOOK_STRATEGIES)
            chosen = HOOK_STRATEGIES[idx]
            logger.info(
                f"🎲 Hook (exploration {total_trials}/{self.EXPLORATION_THRESHOLD}): "
                f"{chosen}"
            )
            return chosen

        # Thompson Sampling
        best_strategy: Optional[str] = None
        best_sample: float = -1.0
        for strategy, stats in self._stats.items():
            alpha = stats.successes + 1
            beta = (stats.trials - stats.successes) + 1
            sample = random.betavariate(alpha, beta)
            if sample > best_sample:
                best_sample = sample
                best_strategy = strategy

        chosen = best_strategy or HOOK_STRATEGIES[0]
        logger.info(
            f"🎯 Hook (bandit): {chosen} "
            f"(p_sample={best_sample:.3f}, "
            f"avg_30s={self._stats[chosen].avg_retention_30s:.1%})"
        )
        return chosen

    def record_performance(
        self,
        episode_number: int,
        strategy: str,
        retention_30s: float,
        retention_60s: float,
        notes: Optional[str] = None,
    ) -> None:
        if strategy not in self._stats:
            logger.warning(f"⚠️ Unknown strategy: {strategy}")
            return

        record = {
            "episode": episode_number,
            "strategy": strategy,
            "retention_30s": retention_30s,
            "retention_60s": retention_60s,
            "timestamp": time.time(),
            "notes": notes or "",
        }
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error(f"❌ Could not record performance: {e}")
            return

        s = self._stats[strategy]
        s.trials += 1
        s.cumulative_retention_30s += retention_30s
        s.cumulative_retention_60s += retention_60s
        if retention_30s >= self.SUCCESS_RETENTION_BAR:
            s.successes += 1

        self._save_stats()
        logger.info(
            f"📊 Recorded ep{episode_number} strategy={strategy} "
            f"r30={retention_30s:.1%} r60={retention_60s:.1%}"
        )

    def _save_stats(self) -> None:
        snapshot = {
            "total_episodes_tracked": sum(s.trials for s in self._stats.values()),
            "strategies": {
                s.strategy: {
                    "trials": s.trials,
                    "successes": s.successes,
                    "avg_retention_30s": s.avg_retention_30s,
                    "avg_retention_60s": s.avg_retention_60s,
                    "success_rate": s.successes / s.trials if s.trials else 0.0,
                }
                for s in self._stats.values()
            },
        }
        try:
            self._stats_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"⚠️ Could not save stats: {e}")

    def report(self) -> Dict:
        return {
            s.strategy: {
                "trials": s.trials,
                "avg_retention_30s": s.avg_retention_30s,
                "success_rate": s.successes / s.trials if s.trials else 0.0,
            }
            for s in self._stats.values()
        }
