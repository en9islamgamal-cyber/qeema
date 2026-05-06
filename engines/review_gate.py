"""
engines/review_gate.py — VALUE / QEEMA v18.0 (NEW — CRITICAL)
=========================================================================
Manual review gate for early-phase episodes.

[Why]
First 10-30 episodes are highest-risk:
- Tafsir validator might miss subtle issues
- Voice settings might not work as expected
- Visual style still being refined
ONE bad early episode can sink channel reputation.

[How it works]
1. Episode goes through script + audio + render normally
2. BEFORE upload, the gate kicks in if episode_number <= REVIEW_THRESHOLD
3. Gate writes a draft summary to: review/episode_{N}_DRAFT.md
4. Sends Telegram message with summary + video link (if configured)
5. Repository status set to "awaiting_review" (not "published")
6. Pipeline EXITS — does NOT upload to YouTube
7. Manual approval flow:
   - Admin reviews video locally
   - Edits temp/episodes/episode_{N}.json if changes needed
   - Re-runs with --episode N --approve to publish

[Telegram notification]
If TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars set:
- Send summary message
- Include script preview, validation results
- Include video file as attachment if < 50MB
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class ReviewVerdict:
    """Result of review gate decision."""
    approved: bool
    reason: str
    review_file: Optional[str] = None


class ReviewGate:
    """Decides if episode can be auto-published or needs manual review."""

    DEFAULT_REVIEW_THRESHOLD: int = 10  # First 10 episodes need review

    def __init__(
        self,
        review_threshold: int = DEFAULT_REVIEW_THRESHOLD,
        force_review: bool = False,
        review_dir: Optional[Path] = None,
    ) -> None:
        self._threshold = review_threshold
        self._force = force_review
        # v18 fix: accept either str or Path for review_dir
        if review_dir is None:
            self._review_dir = Path("review")
        elif isinstance(review_dir, str):
            self._review_dir = Path(review_dir)
        else:
            self._review_dir = review_dir
        self._review_dir.mkdir(parents=True, exist_ok=True)
        # Telegram (optional)
        self._tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")

    def check(
        self,
        episode_number: int,
        script: Any,
        validation_summary: Dict[str, Any],
        video_path: str,
        approval_explicit: bool = False,
    ) -> ReviewVerdict:
        """
        Returns ReviewVerdict.approved=True if pipeline can proceed to upload.

        Args:
            episode_number: 1-based episode number
            script: EpisodeScript object
            validation_summary: dict with quality/tafsir validation results
            video_path: path to the final mp4
            approval_explicit: True if user passed --approve flag (skips gate)
        """
        # Skip all checks if explicitly approved
        if approval_explicit:
            logger.info(f"✅ Review gate: ep{episode_number} explicitly approved")
            return ReviewVerdict(approved=True, reason="explicit --approve flag")

        # Skip if past threshold and not forced
        if not self._force and episode_number > self._threshold:
            return ReviewVerdict(
                approved=True,
                reason=f"ep{episode_number} > threshold {self._threshold}",
            )

        # Need manual review
        review_file = self._write_review_summary(
            episode_number, script, validation_summary, video_path
        )
        self._notify_telegram(episode_number, script, video_path, review_file)
        return ReviewVerdict(
            approved=False,
            reason=f"ep{episode_number} ≤ threshold {self._threshold} — manual review required",
            review_file=review_file,
        )

    def _write_review_summary(
        self,
        episode_number: int,
        script: Any,
        validation: Dict[str, Any],
        video_path: str,
    ) -> str:
        """Write a markdown summary for human review."""
        path = self._review_dir / f"episode_{episode_number:03d}_REVIEW.md"

        try:
            surah = getattr(script, 'surah_name', 'unknown')
            title = getattr(script, 'title', '')
            yt_title = getattr(script, 'youtube_title', '')
            description = getattr(script, 'youtube_description', '')[:500]
            cta = getattr(script, 'cta_text', None) or "(none)"

            intro = ""
            outro = ""
            try:
                intro = getattr(script.intro_scene, 'narrator_text', '')
                outro = getattr(script.outro_scene, 'narrator_text', '')
            except Exception:
                pass

            ayah_summaries: List[str] = []
            try:
                for i, scene in enumerate(script.ayah_scenes, 1):
                    ayah_text = getattr(scene.ayah, 'text', '')
                    hook = getattr(scene, 'hook_text', '')
                    explain = getattr(scene, 'explain_text', '')
                    ayah_summaries.append(
                        f"### آية {i}\n\n"
                        f"**النص:** {ayah_text}\n\n"
                        f"**Hook:** {hook}\n\n"
                        f"**الشرح:** {explain}\n"
                    )
            except Exception as e:
                ayah_summaries.append(f"_Could not extract ayahs: {e}_")

            content = f"""# Review Required — Episode {episode_number}

**Surah:** {surah}  
**Status:** ⏸️ Awaiting manual review  
**Video:** `{video_path}`

---

## 🎬 Metadata

- **Title (Arabic):** {title}
- **YouTube Title:** {yt_title}
- **CTA:** {cta}

### Description Preview
> {description}

---

## 🎤 Intro Hook
> {intro}

## 🎬 Outro
> {outro}

---

## 📖 Per-Ayah Content

{chr(10).join(ayah_summaries)}

---

## ✅ Validation Results

```json
{json.dumps(validation, indent=2, ensure_ascii=False)}
```

---

## 📋 Review Checklist

- [ ] فيديو شغّال بدون مشاكل تقنية
- [ ] الـ hook قوي ومش كليشيه
- [ ] التفسير دقيق دينياً
- [ ] الصور متناسقة بصرياً
- [ ] الصوت طبيعي ومناسب للأطفال
- [ ] الترجمة (subtitles) متطابقة مع الصوت
- [ ] العنوان جذاب ولكن دقيق
- [ ] الـ thumbnail بيعكس المحتوى

---

## 🚀 To approve and publish

```bash
python main.py --episode {episode_number} --approve
```

## ❌ To reject

Edit `temp/episodes/episode_{episode_number:03d}.json` and re-run pipeline.
"""

            path.write_text(content, encoding="utf-8")
            logger.info(f"📋 Review summary written: {path}")
            return str(path)
        except Exception as e:
            logger.error(f"❌ Could not write review summary: {e}")
            return ""

    def _notify_telegram(
        self,
        episode_number: int,
        script: Any,
        video_path: str,
        review_file: str,
    ) -> None:
        """Send Telegram notification (best-effort)."""
        if not self._tg_token or not self._tg_chat:
            logger.info(
                "ℹ️ Telegram not configured — skipping notification "
                "(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)"
            )
            return

        try:
            surah = getattr(script, 'surah_name', '?')
            title = getattr(script, 'youtube_title', '')[:80]

            text = (
                f"⏸ *Episode {episode_number} — Review Required*\n\n"
                f"📖 *Surah:* {surah}\n"
                f"🎬 *Title:* {title}\n\n"
                f"📁 *Video:* `{Path(video_path).name}`\n"
                f"📋 *Review:* `{Path(review_file).name}`\n\n"
                f"To approve:\n"
                f"`python main.py --episode {episode_number} --approve`"
            )

            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            r = requests.post(
                url,
                data={
                    "chat_id": self._tg_chat,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=15,
            )
            if r.status_code == 200:
                logger.info(f"✅ Telegram notification sent for ep{episode_number}")
            else:
                logger.warning(f"⚠️ Telegram returned {r.status_code}: {r.text[:200]}")

            # Try to send video if small enough
            try:
                size_mb = Path(video_path).stat().st_size / (1024 * 1024)
                if size_mb < 50:
                    with open(video_path, "rb") as f:
                        url = f"https://api.telegram.org/bot{self._tg_token}/sendVideo"
                        r = requests.post(
                            url,
                            data={"chat_id": self._tg_chat, "caption": f"Ep {episode_number} preview"},
                            files={"video": f},
                            timeout=120,
                        )
                else:
                    logger.info(
                        f"ℹ️ Video too large for Telegram ({size_mb:.1f}MB) — "
                        "review locally"
                    )
            except Exception as e:
                logger.warning(f"⚠️ Could not send video to Telegram: {e}")

        except Exception as e:
            logger.warning(f"⚠️ Telegram notification failed: {e}")
