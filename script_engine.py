"""
script_engine.py — VALUE / QEEMA v9.0 (Load Balancer & Psychological Engine)
===========================================================================
يقوم بتقسيم الحلقة إلى مهام صغيرة جداً وتوزيعها على كافة مفاتيح الـ API المتاحة.
"""
import json, logging, os, re, requests
from typing import Dict, List, Any, Optional
from config import CURRICULUM, Paths
from models import EpisodeScript, AyahScene, NarratorScene, SceneType, VerifiedAyah
from core_adapters import GeminiAdapter, GroqAdapter

logger = logging.getLogger(__name__)

class ScriptEngine:
    def __init__(self):
        self.adapters = []
        self._setup_load_balancer()
        self.ptr = 0

    def _setup_load_balancer(self):
        # تجميع كل المفاتيح من Gmail 1, 2, 3 و Groq
        keys = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3")]
        for k in keys:
            if k: self.adapters.append((GeminiAdapter(k), "gemini-2.5-flash"))
        if os.getenv("GROQ_API_KEY"):
            self.adapters.append((GroqAdapter(os.getenv("GROQ_API_KEY")), "llama-3.3-70b-versatile"))

        if not self.adapters: raise RuntimeError("❌ لا توجد مفاتيح API!")

    def _call_ai(self, prompt: str, system: str, attempt: int = 1) -> dict:
        """تبديل المفتاح فوراً عند كل طلب لضمان استمرارية الكوتة، مع حماية من التكرار اللانهائي"""
        # إذا جربنا كل المفاتيح المتاحة وفشلت، يجب أن نتوقف ونرمي الخطأ
        if attempt > len(self.adapters):
            logger.error("❌ فشلت جميع مفاتيح الـ API المتاحة!")
            raise RuntimeError("All API keys failed or quota exceeded.")

        adapter, model = self.adapters[self.ptr]
        self.ptr = (self.ptr + 1) % len(self.adapters)

        try:
            res = adapter.generate(prompt, system, model)
            # استخراج الـ JSON بذكاء
            cleaned = re.search(r'\{.*\}', res, re.DOTALL).group()
            return json.loads(cleaned)
        except Exception as e:
            logger.warning(f"⚠️ فشل {model} (المحاولة {attempt}/{len(self.adapters)})، جاري المحاولة مع المفتاح التالي...")
            return self._call_ai(prompt, system, attempt + 1)

    def load_from_disk(self, ep_num: int) -> EpisodeScript | None:
        """
        يتحقق مما إذا كان السكريبت قد تم توليده وحفظه مسبقاً،
        ويقوم بتحميله لتجنب استهلاك الكوتة وإعادة التوليد.
        """
        save_path = Paths.SCRIPT_DIR / f"episode_{ep_num:03d}.json"
        
        if save_path.exists():
            try:
                logger.info(f"✅ تم العثور على سكريبت الحلقة {ep_num} محفوظاً. جاري التحميل...")
                data = json.loads(save_path.read_text(encoding="utf-8"))
                return EpisodeScript(**data)
            except Exception as e:
                logger.error(f"❌ خطأ أثناء قراءة ملف السكريبت للحلقة {ep_num}: {e}")
                return None
        
        logger.info(f"ℹ️ لم يتم العثور على سكريبت مسبق للحلقة {ep_num}. سيتم التوليد من الصفر.")
        return None

    def generate(self, ep_num: int) -> EpisodeScript:
        info = CURRICULUM[ep_num]
        ayahs = self._fetch_ayahs(info)

        system_msg = """أنت 'الجد أبو زياد'، تحكي لأحفادك (5-8 سنوات). 
[الدليل السيكولوجي]: استخدم الترغيب، الحب، القصص الواقعية البسيطة. 
تجنب كلمات العقاب. اجعل الطفل يشعر أن الله يحبه جداً. 
أجب بـ JSON فقط باللغة العربية، والـ visual_prompt بالإنجليزية."""

        logger.info(f"🚀 بدء توليد حلقة سورة {info['name']} بتقنية التجزئة...")

        # 1. المقدمة
        intro_data = self._call_ai(f"اكتب مقدمة دافئة لسورة {info['name']}. أجب بـ JSON: title, youtube_title, youtube_description, intro_text, visual_prompt.", system_msg)

        # 2. الآيات
        ayah_scenes = []
        for i, a in enumerate(ayahs):
            logger.info(f"📖 معالجة الآية {a.number}...")
            prompt = f"الآية: {a.text}. اشرحها للطفل بربطها بموقف جميل في حياته. أجب بـ JSON: intro_text, explain_text, visual_prompt (Detailed Pixar 3D description of the scene)."
            a_data = self._call_ai(prompt, system_msg)
            ayah_scenes.append(AyahScene(scene_id=10+i, ayah=a, intro_text=a_data['intro_text'], explain_text=a_data['explain_text'], visual_prompt=a_data['visual_prompt']))

        # 3. الخاتمة
        outro_data = self._call_ai("اكتب خاتمة الحلقة ودعاء قبل النوم. أجب بـ JSON: narrator_text, visual_prompt.", system_msg)

        script = EpisodeScript(
            episode_number=ep_num, surah_name=info['name'],
            title=intro_data['title'], youtube_title=intro_data['youtube_title'], youtube_description=intro_data['youtube_description'],
            intro_scene=NarratorScene(scene_id=1, scene_type=SceneType.INTRO, narrator_text=intro_data['intro_text'], visual_prompt=intro_data['visual_prompt']),
            ayah_scenes=ayah_scenes,
            outro_scene=NarratorScene(scene_id=99, scene_type=SceneType.OUTRO, narrator_text=outro_data['narrator_text'], visual_prompt=outro_data['visual_prompt'])
        )

        save_path = Paths.SCRIPT_DIR / f"episode_{ep_num:03d}.json"
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return script

    def _fetch_ayahs(self, info):
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            d = requests.get(url).json()
            ayahs.append(VerifiedAyah(surah=info["surah"], number=n, text=d["verse"]["text_uthmani"]))
        return ayahs
