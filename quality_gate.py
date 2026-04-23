import logging
import re
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class QualityReport:
    def __init__(
        self,
        passed: bool,
        overall_score: float,
        section_scores: Dict[str, float],
        critiques: List[str],
    ):
        self.passed = passed
        self.overall_score = overall_score
        self.section_scores = section_scores  # e.g. "pacing", "language", "visual_style", "structure"
        self.critiques = critiques  # رسائل النقد المُفيدة للاستفادة في الإصلاح الذاتي

    def __bool__(self) -> bool:
        return self.passed


class QualityGate:
    """
    بوابة جودة لغوية، بصرية، وهيكية صارمة، مبنية على معايير حلقات يوتيوب للأطفال.
    """

    def __init__(self, min_words: int = 5, max_words: int = 25, enterprise_threshold: float = 85.0):
        self.min_words = min_words
        self.max_words = max_words
        self.enterprise_threshold = enterprise_threshold

        # 1. فخاخ LLM وعبارات "الذكاء الاصطناعي"
        self.forbidden_llm_phrases = [
            "أنا نموذج لغوي",
            "كذكاء اصطناعي",
            "أنا مساعد ذكي",
            "بِسْمِ اللَّهِ",
            "قُلْ هُوَ",
            "آسف",
            "لا يمكنني",
            "بالتأكيد",
            "إليك الـ JSON",
            "أولًا، ثانياً, ثالثًا",
        ]

        # 2. فلتر اللهجة (الحفاظ على مصرية بسيطة ومنتقدة للهجات الدخيلة)
        self.forbidden_dialects = [
            "هيك", "بدي", "شلون", "شو", "زلمة", "وايد", "أبي", "بركي", "هلا والله",
            "ياخي", "نعما", "حنك", "نبكي", "يلا بينك"
        ]

        # 3. مصطلحات اللهجات المصرية المسموحة / المرغوبة في بعض الأحيان (positive hint)
        self.egyptian_touch = [
            "يا حبايبي",
            "يا أبطال",
            "يلا بينا",
            "نركز",
            "حلو",
            "أكيد",
            "أكتر",
            "أنا فاهم",
            "الراجل",
            "البنت",
            "الحاجة",
            "الأسئلة",
            "أحلى",
        ]

        # 4. فلتر الستايل البصري (إنفوجرافيك فقط)
        self.forbidden_visuals = [
            "3d", "pixar", "realistic", "photo", "cinema", "render", "octane", "photography",
            "cinematic", "film", "movie", "character design", "portrait", "realistic photo",
        ]

        # 5. مصطلحات visual style المطلوبة
        self.required_visuals = ["flat", "vector", "infographic", "for kids"]

    def _count_words(self, text: str) -> int:
        """حساب عدد الكلمات غير فارغة"""
        cleaned = re.sub(r"[^ws]", " ", str(text)).strip()
        words = [w for w in cleaned.split() if w and len(w) >= 2]
        return len(words)

    def _contains_ingredient(self, text: str, items: List[str]) -> bool:
        lower = str(text).lower()
        return any(i.lower() in lower for i in items)

    def _check_text_segment(
        self, text: str, segment_name: str, max_penalty: float = 100.0
    ) -> Tuple[float, List[str]]:
        """
        يُحسب الخصم ومجموعة ملاحظات على قطعة نصية واحدة.
        """
        penalty = 0.0
        critiques = []
        word_count = self._count_words(text)

        # 1. الإيقاع / الملل البصري (عدد الكلمات)
        if word_count < self.min_words:
            critiques.append(
                f"[{segment_name}]: قصير جداً ({word_count} كلمات). يجب أن يكون محتوى أعمق قصصياً."
            )
            penalty += 15.0
        elif word_count > self.max_words + 10:  # سماحية 10 كلمات
            critiques.append(
                f"[{segment_name}]: طويل جداً ({word_count} كلمات). هذا يسبب مللاً بصرياً! قصّه أو قسّمه ليكون أقل من {self.max_words} كلمة."
            )
            penalty += 30.0

        # 2. هوية LLM وعبارات "الذكاء الاصطناعي"
        for phrase in self.forbidden_llm_phrases:
            if phrase in text:
                critiques.append(
                    f"[{segment_name}]: يحتوي على عبارة روبوتية غير مقبولة ('{phrase}')."
                )
                penalty += 50.0

        # 3. انحراف لهجوي
        for word in self.forbidden_dialects:
            if f" {word} " in text:
                critiques.append(
                    f"[{segment_name}]: انحراف عن اللهجة المصرية المطلوبة. احذف/غيّر كلمة ('{word}')."
                )
                penalty += 40.0

        # 4. تشكيل مفرط (حماية TTS)
        diacritics = len(re.findall(r"[ً-ْ]", text))
        if diacritics > word_count * 1.5:
            critiques.append(
                f"[{segment_name}]: تشكيل مبالغ فيه ({diacritics} علامة) سيُضعف نطق TTS. احذف التشكيل قدر الإمكان."
            )
            penalty += 20.0

        # 5. وجود لهجة مصرية إيجابية (مكافأة اختيارية، لكن هنا لا نُخصم فقط)
        if not self._contains_ingredient(text, self.egyptian_touch):
            # هذا لا يُخصم درجة، لكن يمكن استخدامه في scoring منفصل إذا أردت
            pass

        # 6. تجنب النص "الجاف" وتشجيع البدايّات والتشويق
        if text.startswith("السورة") or text.startswith("سورة"):
            critiques.append(
                f"[{segment_name}]: يبدأ بجملة جامدة ('السورة …'). حاول البدء بدعوة مباشرة أو سؤال."
            )
            penalty += 10.0

        return min(penalty, max_penalty), critiques

    def _check_visual_segment(
        self, visual_prompt: str, segment_name: str
    ) -> Tuple[float, List[str]]:
        penalty = 0.0
        critiques = []
        lower = str(visual_prompt).lower()

        # 1. عدم وجود الكلمات الضرورية للإنفوجرافيك
        missing = []
        for req in self.required_visuals:
            if req not in lower:
                missing.append(req)
        if missing:
            critiques.append(
                f"[{segment_name} Visual]: يفتقر بعض الكلمات الإرشادية ({', '.join(missing)}). أضفها مثل: 'flat vector graphic, infographic style for kids'."
            )
            penalty += 25.0

        # 2. استخدام أنماط ممنوعة 3D/Photo/Render
        for forbidden in self.forbidden_visuals:
            if forbidden in lower:
                critiques.append(
                    f"[{segment_name} Visual]: استخدم ستايل ممنوع ('{forbidden}'). استخدم بدلاً من ذلك: 'flat vector graphic, infographic'."
                )
                penalty += 40.0

        # 3. وجود نصوص مكتوبة على الصورة (ممنوع في الـquality gate)
        if "no text" not in lower and "no letters" not in lower:
            critiques.append(
                f"[{segment_name} Visual]: الوصف لا يمنع النصوص المكتوبة. أضف 'no text, no letters'."
            )
            penalty += 15.0

        return penalty, critiques

    def _check_structure(self, data: dict) -> Tuple[float, List[str]]:
        """
        فحص الهيكل العام (الحقول الأساسية، وجود المشاهد).
        """
        penalty = 0.0
        critiques = []

        if "title" not in data:
            critiques.append("[Structure] missing 'title'")
            penalty += 10.0

        if "youtube_title" not in data:
            critiques.append("[Structure] missing 'youtube_title'")
            penalty += 10.0

        if "intro_scene" not in data:
            critiques.append("[Structure] missing 'intro_scene'")
            penalty += 20.0
        else:
            intro = data["intro_scene"]
            if "narrator_text" not in intro:
                critiques.append("[Structure] 'intro_scene.narrator_text' missing")
                penalty += 15.0
            if "visual_prompt" not in intro:
                critiques.append("[Structure] 'intro_scene.visual_prompt' missing")
                penalty += 10.0

        if "outro_scene" not in data:
            critiques.append("[Structure] missing 'outro_scene'")
            penalty += 20.0
        else:
            outro = data["outro_scene"]
            if "narrator_text" not in outro:
                critiques.append("[Structure] 'outro_scene.narrator_text' missing")
                penalty += 15.0
            if "visual_prompt" not in outro:
                critiques.append("[Structure] 'outro_scene.visual_prompt' missing")
                penalty += 10.0

        if "ayah_scenes" not in data or not data["ayah_scenes"]:
            critiques.append("[Structure] missing 'ayah_scenes' أو فارغة")
            penalty += 30.0
        else:
            # extra فحص لكل آية
            for i, scene in enumerate(data["ayah_scenes"]):
                prefix = f"[Ayah {i} Field]"
                if not isinstance(scene, dict):
                    critiques.append(f"{prefix} scene not a dict")
                    penalty += 10.0
                elif not scene.get("ayah_number"):
                    critiques.append(f"{prefix} missing ayah_number")
                    penalty += 5.0
                elif not scene.get("intro_text"):
                    critiques.append(f"{prefix} missing intro_text")
                    penalty += 5.0
                elif not scene.get("explain_text"):
                    critiques.append(f"{prefix} missing explain_text")
                    penalty += 5.0
                elif not scene.get("visual_prompt"):
                    critiques.append(f"{prefix} missing visual_prompt")
                    penalty += 5.0
        return penalty, critiques

    def evaluate(self, script_data: Union[str, dict]) -> QualityReport:
        """
        تقييم JSON كامل، وتحديد إن كان يمر من البوابة أم يحتاج إصلاح ذاتي.
        """
        raw = script_data
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception as e:
                logger.warning("Failed to parse JSON for QA: %s", str(e))
                return QualityReport(
                    False,
                    0.0,
                    {"all": 0.0},
                    ["❌ المخرج ليس بصيغة JSON صحيحة (يجب أن يكون Object)."],
                )
        else:
            data = raw

        section_scores: Dict[str, float] = {}
        critiques: List[str] = []

        # 1. فحص الهيكل العام
        struct_penalty, struct_critiques = self._check_structure(data)
        section_scores["structure"] = max(0.0, 100.0 - struct_penalty)
        critiques.extend(struct_critiques)

        # 2. فحص النصوص (الإيقاع + اللغة)
        pacing_penalty = 0.0

        # مشهد المقدمة
        intro = data.get("intro_scene", {})
        p, c = self._check_text_segment(
            intro.get("narrator_text", ""), "intro_scene.narrator_text"
        )
        pacing_penalty += p
        critiques.extend(c)

        # مشهد الخاتمة
        outro = data.get("outro_scene", {})
        p, c = self._check_text_segment(
            outro.get("narrator_text", ""), "outro_scene.narrator_text"
        )
        pacing_penalty += p
        critiques.extend(c)

        # مشاهد الآيات
        for i, scene in enumerate(data.get("ayah_scenes", [])):
            prefix = f"ayah_scenes[{i}]"
            p1, c1 = self._check_text_segment(
                scene.get("intro_text", ""), f"{prefix}.intro_text"
            )
            p2, c2 = self._check_text_segment(
                scene.get("explain_text", ""), f"{prefix}.explain_text"
            )
            pacing_penalty += (p1 + p2) / 2.0
            critiques.extend(c1)
            critiques.extend(c2)

        section_scores["pacing"] = max(0.0, 100.0 - pacing_penalty / 2.0)

        # 3. فحص الأسلوب اللغوي/اللهجة والمحتوى
        lang_penalty = 0.0
        # في نسخة متقدمة، يمكن استخدام معالج لهجة أكثر تعقيدًا، أو محاكاة هوية مساعد الطفل
        # هنا نكتفي بقلة الانتهاكات فقط
        if pacing_penalty > 0:
            lang_penalty += 10.0  # معاقبة النصوص غير المريحة

        section_scores["language"] = max(0.0, 100.0 - lang_penalty)

        # 4. فحص الوصف البصري (الإنفوجرافيك)
        visual_penalty = 0.0

        p1, c1 = self._check_visual_segment(
            intro.get("visual_prompt", ""), "intro_scene.visual_prompt"
        )
        p2, c2 = self._check_visual_segment(
            outro.get("visual_prompt", ""), "outro_scene.visual_prompt"
        )
        visual_penalty += (p1 + p2) / 2.0
        critiques.extend(c1)
        critiques.extend(c2)

        for i, scene in enumerate(data.get("ayah_scenes", [])):
            p, c = self._check_visual_segment(
                scene.get("visual_prompt", ""), f"ayah_scenes[{i}].visual_prompt"
            )
            visual_penalty += p
            critiques.extend(c)

        section_scores["visual_style"] = max(0.0, 100.0 - visual_penalty)

        # 5. درجة إجمالية وقرار القبول
        w = {"structure": 30.0, "pacing": 30.0, "language": 20.0, "visual_style": 20.0}
        total = 0.0
        for k in section_scores:
            weight = w.get(k, 10.0)
            total += section_scores[k] * (weight / 100.0)

        # 6. توليد تقييم نهائي
        passed = total >= self.enterprise_threshold and len(critiques) <= 2

        if not passed:
            logger.warning("❌ Quality Gate Failure: overall_score=%.1f%%", total)
        else:
            logger.info("✅ Quality Gate Passed: overall_score=%.1f%%", total)

        return QualityReport(
            passed, total, section_scores, list(set(critiques))
        )