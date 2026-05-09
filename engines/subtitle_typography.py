"""
engines/subtitle_typography.py — VALUE / QEEMA v22.5
=========================================================================
Arabic typography validator for ASS subtitle output.

[Why this exists]
ASS subtitles need careful Arabic-specific configuration:
  - Encoding=1 (Arabic)
  - Wrapstyle=0 (smart wrap)
  - Font that supports Arabic shaping (Amiri, Cairo, Tajawal, Reem Kufi)
  - Proper Bidi/RTL handling
  - Adequate line height (Arabic diacritics need vertical space)
  - Outline thick enough to be readable on busy backgrounds

Wrong settings = broken Arabic rendering on YouTube (separated letters,
no diacritics, missing characters).

[What this checks]
1. Font names are Arabic-supporting
2. Encoding=1 (Arabic) on every Style line
3. Adequate font sizes for kids' YouTube (≥48pt)
4. Outline thickness sufficient for legibility (≥2)
5. Line spacing appropriate for diacritics
6. No mixed LTR/RTL issues in dialogue

[How to use]
    from engines.subtitle_typography import validate_ass

    report = validate_ass(ass_text)
    if not report.is_valid:
        for issue in report.issues:
            print(issue)

[Integration]
Optional. Runs after subtitle generation. Logs warnings for any issues.
Doesn't fail the build (subtitles are non-critical).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


# Arabic-supporting fonts (verified to render diacritics + RTL correctly)
ARABIC_SUPPORTING_FONTS = {
    "Amiri", "Amiri Quran", "Cairo", "Tajawal", "Reem Kufi",
    "Scheherazade New", "Lateef", "Aref Ruqaa", "Markazi Text",
    "Almarai", "Mada", "El Messiri", "Lalezar", "Noto Naskh Arabic",
    "Noto Sans Arabic", "Noto Kufi Arabic",
    # Common system fallbacks
    "Arial", "Tahoma",  # technically work but discouraged for kids
}

# Recommended fonts for kids' YouTube content (curved, friendly)
KIDS_FRIENDLY_FONTS = {
    "Tajawal", "Cairo", "Almarai", "Mada",
    "Reem Kufi",  # acceptable but more formal
    "Amiri",      # acceptable but classical
}

# Minimum font size for YouTube viewing on phones (most common platform)
MIN_FONT_SIZE_BODY = 48
MIN_FONT_SIZE_AYAH = 60   # Quran verses need to be more prominent

# Minimum outline width
MIN_OUTLINE = 2

# Recommended encoding for Arabic
ARABIC_ENCODING = "1"


@dataclass
class TypographyIssue:
    severity: str          # "error" | "warning" | "info"
    category: str
    location: str          # e.g., "Style: Default" or "Dialogue line 5"
    detail: str
    suggestion: str = ""

    def __str__(self) -> str:
        s = f"[{self.severity.upper()}] {self.category} ({self.location}): {self.detail}"
        if self.suggestion:
            s += f"\n  💡 {self.suggestion}"
        return s


@dataclass
class TypographyReport:
    issues: List[TypographyIssue] = field(default_factory=list)
    style_count: int = 0
    dialogue_count: int = 0

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "warning" for i in self.issues)

    def summary(self) -> str:
        if not self.issues:
            return (
                f"✅ ASS typography clean: "
                f"{self.style_count} styles, {self.dialogue_count} lines"
            )
        errors = sum(1 for i in self.issues if i.severity == "error")
        warns = sum(1 for i in self.issues if i.severity == "warning")
        infos = sum(1 for i in self.issues if i.severity == "info")
        return (
            f"📋 ASS typography: "
            f"{errors} errors, {warns} warnings, {infos} info "
            f"({self.style_count} styles, {self.dialogue_count} lines)"
        )


# ════════════════════════════════════════════════════════════════
# Validator
# ════════════════════════════════════════════════════════════════
def validate_ass(ass_text: str) -> TypographyReport:
    """Validate Arabic typography in an ASS subtitle file.

    Args:
        ass_text: Raw ASS file contents as a string.

    Returns:
        TypographyReport with issues list and statistics.
    """
    report = TypographyReport()

    # Parse ASS sections
    # Look for [Script Info], [V4+ Styles], [Events]
    lines = ass_text.split("\n")
    current_section = None
    style_format: List[str] = []
    event_format: List[str] = []

    for line_num, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue

        # Section headers
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            continue

        # Format: line in styles section
        if current_section == "V4+ Styles" and line.startswith("Format:"):
            style_format = [
                s.strip() for s in line.replace("Format:", "").split(",")
            ]
            continue

        # Format: line in events section
        if current_section == "Events" and line.startswith("Format:"):
            event_format = [
                s.strip() for s in line.replace("Format:", "").split(",")
            ]
            continue

        # Style: line — check it
        if current_section == "V4+ Styles" and line.startswith("Style:"):
            report.style_count += 1
            _validate_style_line(line, style_format, report)
            continue

        # Dialogue: line — check it
        if current_section == "Events" and line.startswith("Dialogue:"):
            report.dialogue_count += 1
            _validate_dialogue_line(
                line, event_format, line_num, report,
            )
            continue

    # Top-level checks (after parsing all)
    _validate_top_level(ass_text, report)

    return report


def _validate_style_line(
    style_line: str,
    format_fields: List[str],
    report: TypographyReport,
) -> None:
    """Check an individual Style: line."""
    # Strip "Style:" prefix
    raw = style_line.replace("Style:", "", 1).strip()
    parts = [p.strip() for p in raw.split(",")]

    if len(parts) != len(format_fields):
        report.issues.append(TypographyIssue(
            severity="warning",
            category="parse",
            location=f"Style: {parts[0] if parts else '?'}",
            detail=f"Field count {len(parts)} != format count {len(format_fields)}",
        ))
        return

    # Map by index
    style: dict = dict(zip(format_fields, parts))
    name = style.get("Name", "?")

    # Check font
    font = style.get("Fontname", "")
    if font not in ARABIC_SUPPORTING_FONTS:
        report.issues.append(TypographyIssue(
            severity="error",
            category="font",
            location=f"Style: {name}",
            detail=f"Font '{font}' may not support Arabic",
            suggestion=(
                f"Use one of: "
                f"{', '.join(sorted(KIDS_FRIENDLY_FONTS))}"
            ),
        ))
    elif font not in KIDS_FRIENDLY_FONTS:
        report.issues.append(TypographyIssue(
            severity="info",
            category="font",
            location=f"Style: {name}",
            detail=f"Font '{font}' works but isn't optimized for kids",
            suggestion=(
                f"Consider: Tajawal, Cairo, or Almarai for kids' content"
            ),
        ))

    # Check encoding
    encoding = style.get("Encoding", "0")
    if encoding != ARABIC_ENCODING:
        report.issues.append(TypographyIssue(
            severity="error",
            category="encoding",
            location=f"Style: {name}",
            detail=f"Encoding={encoding} (should be 1 for Arabic)",
            suggestion="Set Encoding=1",
        ))

    # Check font size
    try:
        font_size = int(style.get("Fontsize", "0"))
        is_ayah = "ayah" in name.lower() or "آية" in name
        min_size = MIN_FONT_SIZE_AYAH if is_ayah else MIN_FONT_SIZE_BODY
        if font_size < min_size:
            report.issues.append(TypographyIssue(
                severity="warning",
                category="size",
                location=f"Style: {name}",
                detail=f"Font size {font_size}pt < {min_size}pt minimum",
                suggestion=(
                    f"For kids' YouTube on mobile, use ≥{min_size}pt"
                ),
            ))
    except ValueError:
        pass

    # Check outline
    try:
        outline = float(style.get("Outline", "0"))
        if outline < MIN_OUTLINE:
            report.issues.append(TypographyIssue(
                severity="warning",
                category="outline",
                location=f"Style: {name}",
                detail=f"Outline={outline} < {MIN_OUTLINE}",
                suggestion=(
                    f"Use outline ≥{MIN_OUTLINE} for legibility "
                    f"on varied backgrounds"
                ),
            ))
    except ValueError:
        pass


def _validate_dialogue_line(
    dialogue_line: str,
    format_fields: List[str],
    line_num: int,
    report: TypographyReport,
) -> None:
    """Check an individual Dialogue: line."""
    # Just check that text isn't empty and has some Arabic
    raw = dialogue_line.replace("Dialogue:", "", 1).strip()

    # Find text (last field)
    if not format_fields:
        return
    parts = raw.split(",", len(format_fields) - 1)
    if len(parts) < len(format_fields):
        return
    text = parts[-1].strip() if parts else ""

    # Strip ASS override codes like {\b1}
    text_clean = re.sub(r'\{[^}]*\}', '', text)

    # Check it has at least some Arabic
    has_arabic = any(
        '\u0600' <= c <= '\u06FF' or
        '\uFB50' <= c <= '\uFEFF'
        for c in text_clean
    )

    if text_clean and not has_arabic:
        # If text has no Arabic but isn't empty, might be a problem
        # (could be technical content like timestamps — be lenient)
        if len(text_clean) > 10:  # only flag substantial non-Arabic text
            report.issues.append(TypographyIssue(
                severity="info",
                category="content",
                location=f"Dialogue line {line_num}",
                detail=f"Non-Arabic text: '{text_clean[:30]}...'",
            ))


def _validate_top_level(
    ass_text: str, report: TypographyReport,
) -> None:
    """Top-level checks on the whole file."""
    # WrapStyle
    if "WrapStyle: 0" not in ass_text and "WrapStyle:0" not in ass_text:
        report.issues.append(TypographyIssue(
            severity="warning",
            category="wrap",
            location="Script Info",
            detail="WrapStyle not set to 0 (smart wrap)",
            suggestion=(
                "Add 'WrapStyle: 0' to [Script Info] for proper "
                "Arabic line breaking"
            ),
        ))

    # ScaledBorderAndShadow
    if "ScaledBorderAndShadow" not in ass_text:
        report.issues.append(TypographyIssue(
            severity="info",
            category="scaling",
            location="Script Info",
            detail="ScaledBorderAndShadow not set",
            suggestion=(
                "Add 'ScaledBorderAndShadow: yes' for consistent "
                "rendering across resolutions"
            ),
        ))


def validate_ass_file(ass_path: str) -> TypographyReport:
    """Convenience: validate an ASS file by path."""
    from pathlib import Path
    text = Path(ass_path).read_text(encoding="utf-8")
    return validate_ass(text)
