"""
script_engine.py — VALUE / QEEMA v2.1
═══════════════════════════════════════════════════════
محرك السكريبت — Production Hardened & Prompt Engineered
• Gemini Pro → Gemini Flash → Cohere → Claude
• النص القرآني: مصدر موثوق فقط
• برومبتات احترافية (Azhari Sheikh Persona + Pixar 3D Prompts)
═══════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from google import genai
from google.genai import types as genai_types

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import cohere
    _COHERE_AVAILABLE = True
except ImportError:
    _COHERE_AVAILABLE = False

from config import APIKeys, CURRICULUM, Paths
from models import (
    AyahScene,
    AudioMood,
    EpisodeScript,
    NarratorScene,
    SceneType,
    VerifiedAyah,
)

logger = logging.getLogger(__name__)


PRIMARY_MODEL = os.getenv("QEEMA_PRIMARY_MODEL", "gemini-2.5-pro")
FALLBACK_MODEL = os.getenv("QEEMA_FALLBACK_MODEL", "gemini-3.1-pro-preview")

USE_COHERE_FALLBACK = os.getenv("QEEMA_USE_COHERE_FALLBACK", "true").lower() == "true"
COHERE_MODEL = os.getenv("QEEMA_COHERE_MODEL", "command-r-plus") 

USE_CLAUDE_FALLBACK = os.getenv("QEEMA_USE_CLAUDE_FALLBACK", "true").lower() == "true"
CLAUDE_MODEL = os.getenv("QEEMA_CLAUDE_MODEL", "claude-opus-4-7")


QURAN_FALLBACK: dict[tuple[int, int], str] = {
    (1, 1): "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ",
    (1, 2): "الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ",
    (1, 3): "الرَّحْمَٰنِ الرَّحِيمِ",
    (1, 4): "مَالِكِ يَوْمِ الدِّينِ",
    (1, 5): "إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ",
    (1, 6): "اهْدِنَا الصِّرَاطَ الْمُسْتَقِيمَ",
    (1, 7): "صِرَاطَ الَّذِينَ أَنْعَمْتَ عَلَيْهِمْ غَيْرِ الْمَغْضُوبِ عَلَيْهِمْ وَلَا الضَّالِّينَ",
    (112, 1): "قُلْ هُوَ اللَّهُ أَحَدٌ",
    (112, 2): "اللَّهُ الصَّمَدُ",
    (112, 3): "لَمْ يَلِدْ وَلَمْ يُولَدْ",
    (112, 4): "وَلَمْ يَكُن لَّهُ كُفُوًا أَحَدٌ",
    (113, 1): "قُلْ أَعُوذُ بِرَبِّ الْفَلَقِ",
    (113, 2): "مِن شَرِّ مَا خَلَقَ",
    (113, 3): "وَمِن شَرِّ غَاسِقٍ إِذَا وَقَبَ",
    (113, 4): "وَمِن شَرِّ النَّفَّاثَاتِ فِي الْعُقَدِ",
    (113, 5): "وَمِن شَرِّ حَاسِدٍ إِذَا حَسَدَ",
    (114, 1): "قُلْ أَعُوذُ بِرَبِّ النَّاسِ",
    (114, 2): "مَلِكِ النَّاسِ",
    (114, 3): "إِلَٰهِ النَّاسِ",
    (114, 4): "مِن شَرِّ الْوَسْوَاسِ الْخَنَّاسِ",
    (114, 5): "الَّذِي يُوَسْوِسُ فِي صُدُورِ النَّاسِ",
    (114, 6): "مِنَ الْجِنَّةِ وَالنَّاسِ",
    (110, 1): "إِذَا جَاءَ نَصْرُ اللَّهِ وَالْفَتْحُ",
    (110, 2): "وَرَأَيْتَ النَّاسَ يَدْخُلُونَ فِي دِينِ اللَّهِ أَفْوَاجًا",
    (110, 3): "فَسَبِّحْ بِحَمْدِ رَبِّكَ وَاسْتَغْفِرْهُ ۚ إِنَّهُ كَانَ تَوَّابًا",
    (108, 1): "إِنَّا أَعْطَيْنَاكَ الْكَوْثَرَ",
    (108, 2): "فَصَلِّ لِرَبِّكَ وَانْحَرْ",
    (108, 3): "إِنَّ شَانِئَكَ هُوَ الْأَبْتَرُ",
    (103, 1): "وَالْعَصْرِ",
    (103, 2): "إِنَّ الْإِنسَانَ لَفِي خُسْرٍ",
    (103, 3): "إِلَّا الَّذِينَ آمَنُوا وَعَمِلُوا الصَّالِحَاتِ وَتَوَاصَوْا بِالْحَقِّ وَتَوَاصَوْا بِالصَّبْرِ",
    (97, 1): "إِنَّا أَنزَلْنَاهُ فِي لَيْلَةِ الْقَدْرِ",
    (97, 2): "وَمَا أَدْرَاكَ مَا لَيْلَةُ الْقَدْرِ",
    (97, 3): "لَيْلَةُ الْقَدْرِ خَيْرٌ مِّنْ أَلْفِ شَهْرٍ",
    (97, 4): "تَنَزَّلُ الْمَلَائِكَةُ وَالرُّوحُ فِيهَا بِإِذْنِ رَبِّهِم مِّن كُلِّ أَمْرٍ",
    (97, 5): "سَلَامٌ هِيَ حَتَّىٰ مَطْلَعِ الْفَجْرِ",
}


class QuranTextFetcher:
    API_URL = "https://api.qurancdn.com/api/qdc/verses/by_key/{surah}:{ayah}?words=false&fields=text_uthmani"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def fetch(self, surah: int, ayah: int) -> str:
        try:
            resp = requests.get(self.API_URL.format(surah=surah, ayah=ayah), timeout=10)
            resp.raise_for_status()
            text = resp.json()["verse"]["text_uthmani"]
            logger.info(f"✅ نص الآية {surah}:{ayah} من API")
            return text
        except Exception as e:
            fallback = QURAN_FALLBACK.get((surah, ayah))
            if fallback:
                logger.warning(f"⚠️ API فشل ({e})، استخدام النص الاحتياطي")
                return fallback
            raise ValueError(f"لا يوجد نص للآية {surah}:{ayah}: {e}")

    def fetch_surah(self, surah: int, start: int, end: int) -> list[VerifiedAyah]:
        ayahs = []
        for n in range(start, end + 1):
            text = self.fetch(surah, n)
            ayahs.append(VerifiedAyah(surah=surah, number=n, text=text, source="quran_api"))
        return ayahs


class QuranIntegrityGuard:
    FORBIDDEN_IN_NARRATOR = [
        r"بِسْمِ اللَّهِ", r"الْحَمْدُ لِلَّهِ", r"قُلْ هُوَ اللَّهُ", 
        r"إِنَّا أَعْطَيْنَاكَ", r"وَالْعَصْرِ", r"قُلْ أَعُوذُ",
    ]

    def run(self, script: EpisodeScript, verified_ayahs: list[VerifiedAyah]) -> None:
        self._check_1_no_quran_in_narrator(script)
        self._check_2_all_ayahs_verified(script, verified_ayahs)
        self._check_3_ayah_texts_match(script, verified_ayahs)
        self._check_4_no_placeholder_leaked(script)
        self._check_5_source_tags(script)
        logger.info("🛡️ 5/5 طبقات حماية — النص نظيف")

    def _check_1_no_quran_in_narrator(self, script: EpisodeScript):
        all_texts = [script.intro_scene.narrator_text, script.outro_scene.narrator_text]
        for s in script.mid_scenes: all_texts.append(s.narrator_text)
        for a in script.ayah_scenes: all_texts += [a.intro_text, a.explain_text]
        for text in all_texts:
            for pattern in self.FORBIDDEN_IN_NARRATOR:
                if re.search(pattern, text):
                    raise ValueError(f"🚨 [L1] نص قرآني في كلام الراوي: {text[:60]}")

    def _check_2_all_ayahs_verified(self, script: EpisodeScript, verified: list[VerifiedAyah]):
        verified_nums = {a.number for a in verified}
        for scene in script.ayah_scenes:
            if scene.ayah.number not in verified_nums:
                raise ValueError(f"🚨 [L2] الآية {scene.ayah.number} غير موجودة")
            if scene.ayah.source != "quran_api":
                raise ValueError(f"🚨 [L2] الآية {scene.ayah.number} مصدرها غير موثوق")

    def _check_3_ayah_texts_match(self, script: EpisodeScript, verified: list[VerifiedAyah]):
        verified_map = {a.number: a.text for a in verified}
        for scene in script.ayah_scenes:
            expected = verified_map.get(scene.ayah.number)
            if expected and scene.ayah.text != expected:
                raise ValueError(f"🚨 [L3] نص الآية {scene.ayah.number} لا يطابق المصدر")

    def _check_4_no_placeholder_leaked(self, script: EpisodeScript):
        for scene in script.ayah_scenes:
            if "[AYAH" in scene.ayah.text or "PLACEHOLDER" in scene.ayah.text.upper():
                raise ValueError(f"🚨 [L4] Placeholder غير محقق في الآية {scene.ayah.number}")

    def _check_5_source_tags(self, script: EpisodeScript):
        for scene in script.ayah_scenes:
            if not scene.ayah.source:
                raise ValueError(f"🚨 [L5] آية {scene.ayah.number} بدون source tag")


class ScriptEngine:
    SYSTEM_PROMPT = """أنت "الجد أبو زياد"، عالم من علماء الأزهر الشريف، ذو وجه بشوش وصوت دافئ وحنون.
مهمتك: تقديم حلقات كرتونية تعليمية للأطفال (سن 5 إلى 8 سنوات) لتفسير القرآن الكريم بأسلوب قصصي شيق.

قواعد الإخراج والسرد (يجب الالتزام بها حرفياً):
1. السرد المتصل: أنت لا تفسر الآيات بشكل منفصل وممل. أنت تحكي "قصة واحدة مترابطة" من بداية السورة لنهايتها. استخدم الآيات كجزء من سياق القصة لتسهيل فهمها وحفظها.
2. اللهجة والأداء: مصري عامي راقي ومبسط جداً. استخدم كلمات مثل (يا حبايبي، شوفوا يا أبطال، سبحان الله). الجمل الصوتية (narrator_text و explain_text) يجب أن تكون قصيرة جداً لسهولة استيعاب الأطفال.
3. هندسة الصور (Visual Prompts): يجب أن تكون باللغة الإنجليزية، دقيقة، وبأسلوب رسوم متحركة عالمي. 
   - استخدم دائماً مفاتيح مثل: "Cute 3D Pixar style, Disney animation style, vibrant colors, soft lighting, highly detailed, 8k, Islamic kid friendly --no text".
   - صف المشهد بوضوح (مثال: A cute little Egyptian boy looking at stars in a beautifully lit night sky, Pixar style).
4. الحماية القرآنية (صارم جداً): يُمنع منعاً باتاً كتابة أي نص قرآني في مخرجاتك. استخدم فقط المعرفات مثل [AYAH_1] في مكانها المخصص.
5. الإخراج التقني: أجب بصيغة JSON صحيحة 100% فقط، بدون أي شروحات خارجية أو علامات Markdown."""

    def __init__(self):
        if not APIKeys.GEMINI:
            raise ValueError("GEMINI_API_KEY غير موجود")

        self.gemini_client = genai.Client(api_key=APIKeys.GEMINI)

        self.cohere_client: Optional["cohere.Client"] = None
        if USE_COHERE_FALLBACK and _COHERE_AVAILABLE:
            cohere_key = os.getenv("COHERE_API_KEY")
            if cohere_key:
                self.cohere_client = cohere.Client(api_key=cohere_key)
                logger.info(f"✅ Cohere fallback جاهز ({COHERE_MODEL})")

        self.claude_client: Optional["anthropic.Anthropic"] = None
        if USE_CLAUDE_FALLBACK and _ANTHROPIC_AVAILABLE:
            anthropic_key = os.getenv("ANTHROPIC_API_KEY")
            if anthropic_key:
                self.claude_client = anthropic.Anthropic(api_key=anthropic_key)
                logger.info(f"✅ Claude fallback جاهز ({CLAUDE_MODEL})")

        self.text_fetcher = QuranTextFetcher()
        self.guard        = QuranIntegrityGuard()

    def generate(self, episode_num: int) -> EpisodeScript:
        if episode_num not in CURRICULUM:
            raise ValueError(f"حلقة {episode_num} غير موجودة في المنهج")

        info    = CURRICULUM[episode_num]
        surah   = info["surah"]
        sname   = info["name"]
        s_start = info["start"]
        s_end   = info["end"]
        n_ayahs = s_end - s_start + 1

        logger.info(f"📖 جلب {n_ayahs} آيات من سورة {sname}…")
        verified_ayahs = self.text_fetcher.fetch_surah(surah, s_start, s_end)

        ayah_refs = "\n".join([
            f"[AYAH_{a.number}] — (الآية {a.number} - لا تكتب نصها)"
            for a in verified_ayahs
        ])

        prompt = f"""المطلوب: إنتاج سكريبت حلقة كرتونية كاملة للجد أبو زياد، يشرح فيها سورة {sname} من الآية {s_start} إلى الآية {s_end}.
اجعل شرح هذه الآيات عبارة عن رحلة أو قصة واحدة متصلة وممتعة، بحيث تمهد كل آية للتي تليها بسلاسة.

المعرفات القرآنية المسموح باستخدامها (يمنع كتابة النصوص الحقيقية):
{ayah_refs}

اكتب بـ JSON بهذا الشكل بالضبط:
{{
  "title": "عنوان جذاب للحلقة",
  "youtube_title": "عنوان يوتيوب 70 حرفاً",
  "youtube_description": "وصف 200 كلمة بالعربية",
  "youtube_tags": ["تاج1","تاج2","تاج3","تاج4","تاج5"],
  "total_duration_sec": 300,
  "intro_scene": {{
    "scene_id": 1,
    "duration_sec": 25,
    "narrator_text": "ترحيب حار وقصصي من الجد أبو زياد يمهد لموضوع السورة ككل",
    "visual_prompt": "English prompt for image generator (Pixar style...)",
    "on_screen_text": "نص قصير على الشاشة",
    "mood": "intro"
  }},
  "ayah_scenes": [
    {{
      "scene_id": 10,
      "ayah_ref": "[AYAH_1]",
      "ayah_number": 1,
      "intro_text": "الجد يربط القصة بالآية ويمهد لسماعها",
      "explain_text": "شرح مبسط جداً للآية يخدم القصة العامة",
      "visual_prompt": "English prompt for AI image (Pixar style...)",
      "repetitions": 3,
      "duration_sec": 35
    }}
  ],
  "mid_scenes": [],
  "outro_scene": {{
    "scene_id": 99,
    "duration_sec": 20,
    "narrator_text": "خاتمة للقصة واستخلاص العبرة والتشجيع",
    "visual_prompt": "English prompt (Pixar style...)",
    "on_screen_text": "اشترك وفعّل الجرس 🔔",
    "mood": "outro"
  }}
}}"""

        data = self._call_ai_with_fallback(prompt)

        script = self._build_script(episode_num, info, data, verified_ayahs)
        self.guard.run(script, verified_ayahs)

        script_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"💾 سكريبت محفوظ: {script_path.name}")
        return script

    def _call_ai_with_fallback(self, prompt: str) -> dict:
        models_queue = []
        if PRIMARY_MODEL: models_queue.append((PRIMARY_MODEL, self._call_gemini))
        if FALLBACK_MODEL and FALLBACK_MODEL != PRIMARY_MODEL: models_queue.append((FALLBACK_MODEL, self._call_gemini))
        models_queue.append(("gemini-2.5-flash", self._call_gemini))
        if self.cohere_client is not None: models_queue.append((COHERE_MODEL, self._call_cohere))
        if self.claude_client is not None: models_queue.append((CLAUDE_MODEL, self._call_claude))

        errors: list[str] = []

        for model_name, call_func in models_queue:
            logger.info(f"🤖 جاري المحاولة باستخدام: {model_name}...")
            max_retries = 5 
            base_wait = 10 

            for attempt in range(max_retries):
                try:
                    if call_func == self._call_gemini:
                        raw = call_func(model_name, prompt)
                    else:
                        raw = call_func(prompt)
                    return self._parse_json(raw)
                    
                except Exception as e:
                    error_msg = str(e)
                    if "404" in error_msg or "removed" in error_msg.lower() or "not found" in error_msg.lower():
                        logger.error(f"❌ النموذج {model_name} غير موجود. تخطي.")
                        errors.append(f"{model_name}: Model Not Found")
                        break 
                    elif "quota" in error_msg.lower():
                        logger.error(f"❌ نفاذ الحصة لنموذج {model_name}. تخطي.")
                        errors.append(f"{model_name}: Quota Exhausted")
                        break 
                    elif any(err in error_msg for err in ["429", "503", "500", "502", "Too Many Requests"]):
                        if attempt < max_retries - 1:
                            wait_time = base_wait * (2 ** attempt)
                            logger.warning(f"⚠️ ضغط خوادم ({model_name}). ننتظر {wait_time}ث...")
                            time.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"❌ استنفاد محاولات {model_name}.")
                            errors.append(f"{model_name}: Rate Limit Exhausted")
                            break 
                    elif "credit balance" in error_msg.lower() or "billing" in error_msg.lower():
                        logger.error(f"❌ خطأ مالي في {model_name}. تخطي.")
                        errors.append(f"{model_name}: Insufficient Credits")
                        break 
                    else:
                        logger.error(f"❌ خطأ غير متوقع في {model_name}: {error_msg}")
                        errors.append(f"{model_name}: {type(e).__name__} - {error_msg[:100]}")
                        break

        raise RuntimeError("فشلت كل الموديلات:\n  - " + "\n  - ".join(errors))

    def _call_gemini(self, model: str, prompt: str) -> str:
        config = genai_types.GenerateContentConfig(
            temperature=0.75, 
            max_output_tokens=6000,
            system_instruction=self.SYSTEM_PROMPT,
        )
        response = self.gemini_client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        text = (response.text or "").strip()
        if not text: raise RuntimeError(f"استجابة فارغة من {model}")
        return text
        
    def _call_cohere(self, prompt: str) -> str:
        assert self.cohere_client is not None
        response = self.cohere_client.chat(
            message=prompt,
            preamble=self.SYSTEM_PROMPT,
            model=COHERE_MODEL,
            temperature=0.75
        )
        text = response.text.strip()
        if not text: raise RuntimeError(f"استجابة فارغة من {COHERE_MODEL}")
        return text

    def _call_claude(self, prompt: str) -> str:
        assert self.claude_client is not None
        message = self.claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=6000,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.75
        )
        text = message.content[0].text.strip()
        if not text: raise RuntimeError(f"استجابة فارغة من {CLAUDE_MODEL}")
        return text

    @staticmethod
    def _parse_json(raw: str) -> dict:
        cleaned = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*\x60{3}$", "", cleaned, flags=re.MULTILINE)
        return json.loads(cleaned)

    def _build_script(
        self,
        ep_num: int,
        info: dict,
        data: dict,
        verified_ayahs: list[VerifiedAyah],
    ) -> EpisodeScript:
        ayah_map = {a.number: a for a in verified_ayahs}

        intro_raw = data["intro_scene"]
        intro = NarratorScene(
            scene_id=intro_raw["scene_id"],
            scene_type=SceneType.INTRO,
            duration_sec=float(intro_raw.get("duration_sec", 25)),
            narrator_text=intro_raw["narrator_text"],
            visual_prompt=intro_raw["visual_prompt"],
            on_screen_text=intro_raw.get("on_screen_text"),
            mood=AudioMood(intro_raw.get("mood", "intro")),
        )

        ayah_scenes = []
        for i, raw in enumerate(data.get("ayah_scenes", [])):
            ayah_num = raw["ayah_number"]
            if ayah_num not in ayah_map: raise ValueError(f"الآية {ayah_num} غير موجودة")

            ayah_scenes.append(AyahScene(
                scene_id=raw["scene_id"],
                ayah=ayah_map[ayah_num],
                intro_text=raw["intro_text"],
                explain_text=raw["explain_text"],
                visual_prompt=raw["visual_prompt"],
                repetitions=int(raw.get("repetitions", 3)),
                duration_sec=float(raw.get("duration_sec", 35)),
            ))

        mid_scenes = []
        for raw in data.get("mid_scenes", []):
            mid_scenes.append(NarratorScene(
                scene_id=raw["scene_id"],
                scene_type=SceneType.EXPLANATION,
                duration_sec=float(raw.get("duration_sec", 20)),
                narrator_text=raw["narrator_text"],
                visual_prompt=raw["visual_prompt"],
                mood=AudioMood(raw.get("mood", "calm")),
            ))

        outro_raw = data["outro_scene"]
        outro = NarratorScene(
            scene_id=outro_raw["scene_id"],
            scene_type=SceneType.OUTRO,
            duration_sec=float(outro_raw.get("duration_sec", 20)),
            narrator_text=outro_raw["narrator_text"],
            visual_prompt=outro_raw["visual_prompt"],
            on_screen_text=outro_raw.get("on_screen_text"),
            mood=AudioMood("outro"),
        )

        from config import ChannelConfig
        return EpisodeScript(
            episode_number=ep_num,
            surah_name=info["name"],
            surah_number=info["surah"],
            title=data["title"],
            youtube_title=data["youtube_title"],
            youtube_description=data["youtube_description"],
            youtube_tags=data.get("youtube_tags", []) + ChannelConfig.BASE_TAGS,
            total_duration_sec=float(data.get("total_duration_sec", 300)),
            intro_scene=intro,
            ayah_scenes=ayah_scenes,
            mid_scenes=mid_scenes,
            outro_scene=outro,
        )

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            logger.info(f"♻️ استئناف سكريبت: {p.name}")
            return EpisodeScript.model_validate(data)
        return None
