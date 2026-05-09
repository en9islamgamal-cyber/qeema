"""Tests for ForbiddenAnalogyDetector (v22.6).

This is a deterministic keyword-level pre-check that catches the four
canonical doctrinal errors before content reaches the Gemini reviewer.
The detector is deliberately conservative: it only fires when:

  1. The ayah's TOPIC matches one of the rules (so we don't flag academic
     mentions of magnets in a discussion of nature)
  AND
  2. The explanation/analogy contains a forbidden KEYWORD pattern

Coverage:
  - Each of the 4 rules fires on canonical positive case
  - Each rule does NOT fire when the topic doesn't match
  - Each rule does NOT fire when the keyword isn't present
  - Arabic diacritic variations (tashkeel) don't break matching
  - Whitespace / casing variations don't break matching
  - Empty inputs are handled defensively
  - Multiple rules can fire simultaneously
"""
from __future__ import annotations

import pytest

from engines.tafsir_validator import ForbiddenAnalogyDetector


# ════════════════════════════════════════════════════════════════
# Rule #1 — Judgment day as biology
# ════════════════════════════════════════════════════════════════
class TestJudgmentDayAsBiology:

    def test_fires_when_judgment_day_paired_with_heartbeat(self):
        """The canonical bad analogy: 'مالك يوم الدين' explained as
        a body process like a heartbeat."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="مالك يوم الدين",
            explanation="ربنا هو الملك في يوم الحساب",
            analogy="زي نبض القلب اللي مستمر طول العمر",
        )
        assert len(concerns) == 1
        assert "judgment-day-as-biology" in concerns[0]
        assert "نبض" in concerns[0]

    def test_fires_on_cellular_keyword(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إذا زلزلت الأرض زلزالها",
            explanation="يوم القيامة",
            analogy="زي عمل الخلايا اللي بيحصل في الجسم على طول",
        )
        assert any("judgment-day-as-biology" in c for c in concerns)

    def test_does_not_fire_when_ayah_isnt_about_judgment_day(self):
        """Magnetic field mentioned in a nature discussion is fine — the
        rule should NOT fire because the ayah's topic isn't about judgment."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="والشمس وضحاها",
            explanation="الشمس آية من آيات الله",
            analogy="زي الخلايا اللي بتشتغل في النبات لتحويل الضوء",
        )
        assert not any("judgment-day-as-biology" in c for c in concerns)

    def test_does_not_fire_when_no_forbidden_keyword(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="مالك يوم الدين",
            explanation="ربنا هو الملك في يوم الحساب",
            analogy="زي يوم الامتحان لما الكل بيستنى نتيجة شغله",
        )
        assert not any("judgment-day-as-biology" in c for c in concerns)


# ════════════════════════════════════════════════════════════════
# Rule #2 — Worship as magnet
# ════════════════════════════════════════════════════════════════
class TestWorshipAsMagnet:

    def test_fires_on_magnet_paired_with_iyaka_naabud(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد وإياك نستعين",
            explanation="نعبد الله وحده",
            analogy="زي المغناطيس اللي بيشد الحديد، إحنا منجذبين لربنا",
        )
        assert len(concerns) == 1
        assert "worship-as-magnet" in concerns[0]
        assert "مغناطيس" in concerns[0]

    def test_fires_on_physical_attraction_keyword(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد وإياك نستعين",
            explanation="العبادة الحقة لله",
            analogy="بنشد لربنا بالانجذاب الفيزيائي زي الأشياء للأرض",
        )
        assert any("worship-as-magnet" in c for c in concerns)

    def test_does_not_fire_when_topic_isnt_worship(self):
        """Magnet as an example of physics in a nature ayah is fine."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="ألم تر إلى ربك كيف مد الظل",
            explanation="من آيات الله امتداد الظلال",
            analogy="زي المغناطيس اللي بيظهر قواه من غير ما نشوفها",
        )
        assert not any("worship-as-magnet" in c for c in concerns)


# ════════════════════════════════════════════════════════════════
# Rule #3 — Wrath as food
# ════════════════════════════════════════════════════════════════
class TestWrathAsFood:

    def test_fires_on_healthy_unhealthy_food_pairing(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="غير المغضوب عليهم ولا الضالين",
            explanation="نسأل الله أن يبعدنا عن طريق المغضوب عليهم",
            analogy="زي الفرق بين الأكل الصحي وأكل غير صحي للجسم",
        )
        assert any("wrath-as-food" in c for c in concerns)

    def test_does_not_fire_for_food_in_provision_ayah(self):
        """Food talk in a رزق ayah is appropriate."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="فلينظر الإنسان إلى طعامه",
            explanation="ربنا أنعم علينا بالأكل",
            analogy="الأكل الصحي نعمة من ربنا",
        )
        assert not any("wrath-as-food" in c for c in concerns)


# ════════════════════════════════════════════════════════════════
# Rule #4 — Basmala as magic word
# ════════════════════════════════════════════════════════════════
class TestBasmalaAsMagic:

    def test_fires_on_secret_code_description(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله الرحمن الرحيم",
            explanation="نبدأ كل شيء بهذه الكلمة",
            analogy="بسم الله زي كود سري بيفتح أبواب الخير",
        )
        assert any("basmala-as-magic" in c for c in concerns)

    def test_fires_on_magic_word_description(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله الرحمن الرحيم",
            explanation="...",
            analogy="هي كلمة سحرية بتجيب البركة",
        )
        assert any("basmala-as-magic" in c for c in concerns)

    def test_fires_on_english_magic_keyword(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله الرحمن الرحيم",
            explanation="...",
            analogy="It's like a magic word for blessings",
        )
        assert any("basmala-as-magic" in c for c in concerns)

    def test_passes_correct_basmala_explanation(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله الرحمن الرحيم",
            explanation="نبدأ بها لطلب البركة من الله",
            analogy="زي ما بنبدأ كل عمل بأهم اسم — اسم ربنا",
        )
        assert not any("basmala-as-magic" in c for c in concerns)


# ════════════════════════════════════════════════════════════════
# Arabic morphology / normalization
# ════════════════════════════════════════════════════════════════
class TestArabicNormalization:

    def test_diacritics_in_ayah_dont_block_topic_match(self):
        """Quranic text comes with tashkeel; the rule's topic_kws are
        unvocalized. Normalization must strip tashkeel from the input."""
        concerns = ForbiddenAnalogyDetector.check(
            # Vocalized basmala (typical from quran.com API)
            ayah_text="بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ",
            explanation="...",
            analogy="هي كلمة سحرية",
        )
        assert any("basmala-as-magic" in c for c in concerns)

    def test_diacritics_in_explanation_dont_block_keyword_match(self):
        """If the LLM emits vocalized text, the keyword check should still
        fire."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد",
            explanation="العبادة لله وحده",
            analogy="مَغْنَاطِيس بيشد الحديد",  # vocalized magnet
        )
        assert any("worship-as-magnet" in c for c in concerns)

    def test_extra_whitespace_doesnt_block_match(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="مالك  يوم   الدين",  # extra spaces
            explanation="...",
            analogy="زي    نبض    القلب",
        )
        assert any("judgment-day-as-biology" in c for c in concerns)

    def test_case_folding_for_english_keywords(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله الرحمن الرحيم",
            explanation="...",
            analogy="A SECRET CODE that brings barakah",
        )
        assert any("basmala-as-magic" in c for c in concerns)


# ════════════════════════════════════════════════════════════════
# Edge cases & defensive behaviour
# ════════════════════════════════════════════════════════════════
class TestEdgeCases:

    def test_all_empty_returns_empty(self):
        assert ForbiddenAnalogyDetector.check(
            ayah_text="", explanation="", analogy="",
        ) == []

    def test_empty_explanation_doesnt_crash(self):
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="بسم الله", explanation="", analogy="",
        )
        # No keyword to match → no concerns
        assert concerns == []

    def test_clean_kid_friendly_content_passes(self):
        """The realistic happy path: a good v22.6 batch script for the
        whole Fatiha should yield zero concerns."""
        ayahs = [
            ("بسم الله الرحمن الرحيم",
             "نبدأ بها لطلب البركة من الله",
             "زي ما بنبدأ كل عمل بأهم اسم — اسم ربنا"),
            ("الحمد لله رب العالمين",
             "نشكر الله الذي خلق كل شيء",
             "زي ما الطفل يشكر أمه على نعمها"),
            ("الرحمن الرحيم",
             "ربنا واسع الرحمة بكل عباده",
             "زي الشمس اللي بتدفي الكل بدون استثناء"),
            ("مالك يوم الدين",
             "ربنا هو الملك في يوم الحساب",
             "زي يوم الامتحان حين تظهر النتائج"),
            ("إياك نعبد وإياك نستعين",
             "نعبد الله وحده ونطلب عونه وحده",
             "زي ما الطفل يطلب من والديه أولاً قبل غيرهما"),
            ("اهدنا الصراط المستقيم",
             "نطلب من ربنا أن يهدينا الطريق",
             "زي الخريطة اللي بتدل الطريق الصحيح"),
            ("صراط الذين أنعمت عليهم",
             "طريق الذين أنعم الله عليهم بالهداية",
             "زي الأنبياء والصالحين الذين اتبعوا الحق"),
        ]
        for ayah_text, expl, anal in ayahs:
            concerns = ForbiddenAnalogyDetector.check(
                ayah_text=ayah_text, explanation=expl, analogy=anal,
            )
            assert concerns == [], (
                f"Clean content for «{ayah_text}» wrongly flagged: {concerns}"
            )

    def test_multiple_rules_can_fire_at_once(self):
        """An exceptionally bad explanation could trigger multiple rules."""
        # Pathological case: explanation mixes basmala + worship topics
        # AND uses both magnet and magic-word language.
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد بسم الله",  # both topics
            explanation="...",
            analogy="زي مغناطيس + كلمة سحرية",  # both forbidden
        )
        rule_ids = {c.split("]")[0].lstrip("[") for c in concerns}
        # Both rules fire
        assert "worship-as-magnet" in rule_ids
        assert "basmala-as-magic" in rule_ids

    def test_concern_text_is_in_arabic(self):
        """Concerns surface in logs and the validation report — they MUST
        be in Arabic to be useful to the bilingual reviewer."""
        concerns = ForbiddenAnalogyDetector.check(
            ayah_text="إياك نعبد",
            explanation="...",
            analogy="زي المغناطيس",
        )
        assert concerns
        # Each concern must contain at least some Arabic text
        for c in concerns:
            arabic_chars = [ch for ch in c if "\u0600" <= ch <= "\u06FF"]
            assert len(arabic_chars) > 5, (
                f"Concern is mostly non-Arabic: {c!r}"
            )

    def test_rule_id_appears_in_concern_for_traceability(self):
        """When a concern is logged, devs need to know WHICH rule fired."""
        for ayah, expl, anal, expected_rule in [
            ("مالك يوم الدين", "...", "زي نبض القلب", "judgment-day-as-biology"),
            ("إياك نعبد", "...", "زي المغناطيس", "worship-as-magnet"),
            ("المغضوب عليهم", "...", "أكل صحي وأكل غير صحي", "wrath-as-food"),
            ("بسم الله", "...", "كود سري", "basmala-as-magic"),
        ]:
            concerns = ForbiddenAnalogyDetector.check(
                ayah_text=ayah, explanation=expl, analogy=anal,
            )
            assert any(expected_rule in c for c in concerns), (
                f"Rule {expected_rule} did not fire on its canonical positive case. "
                f"Got: {concerns}"
            )
