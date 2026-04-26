"""
script_engine.py — VALUE / QEEMA v8.0 (Distributed Load Balancer & Psychological Engine)
========================================================================================
يتميز هذا المحرك بالتالي:
1. Load Balancing: تبديل ذكي بين مفاتيح الـ API المتاحة (Gemini Flash, Groq, Cohere) لعدم تجاوز الكوتة.
2. Chunking: تجزئة بناء السكريبت (المقدمة، ثم الآيات تباعاً، ثم الخاتمة) لضمان الاستفاضة في الشرح.
3. Psychological Prompts: توجيهات صارمة لمنع كلمات التخويف والتركيز على التربية الإيجابية.
4. Robust Parsing: خوارزمية صلبة لاستخراج الـ JSON مهما أضاف النموذج من نصوص جانبية.
"""

import json
import logging
import os
import re
import requests
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential

from config import CURRICULUM, Paths
from models import EpisodeScript, AyahScene, NarratorScene, SceneType, AudioMood, VerifiedAyah
from core_adapters import GeminiAdapter, GroqAdapter, CohereAdapter

logger = logging.getLogger(__name__)

class ScriptEngine:
    def __init__(self):
        self.adapters = []
        self._init_adapters()
        self.current_adapter_idx = 0

    def _init_adapters(self):
        """تهيئة قائمة محولات النماذج المتاحة للتبديل بينها (Round Robin)"""
        # 1. إعداد مفاتيح Gemini (ممتازة للسرعة والتكلفة)
        gemini_keys = [
            os.getenv("GEMINI_API_KEY"), 
            os.getenv("GEMINI_API_KEY_2"), 
            os.getenv("GEMINI_API_KEY_3")
        ]
        for key in gemini_keys:
            if key: 
                self.adapters.append((GeminiAdapter(key), "gemini-2.5-flash"))
                
        # 2. إعداد مفتاح Groq (ممتاز للاستنتاج اللغوي العميق)
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            self.adapters.append((GroqAdapter(groq_key), "llama-3.3-70b-versatile"))
            
        if not self.adapters:
            raise ValueError("❌ خطأ قاتل: لا توجد مفاتيح API متاحة في متغيرات البيئة لبناء السكريبت.")
            
        logger.info(f"✅ Load Balancer جاهز ويعمل بـ {len(self.adapters)} نقطة وصول (Endpoints).")

    def _get_next_adapter(self):
        """خوارزمية Round Robin لاختيار النموذج التالي وتوزيع الحمل."""
        adapter, model = self.adapters[self.current_adapter_idx]
        self.current_adapter_idx = (self.current_adapter_idx + 1) % len(self.adapters)
        return adapter, model

    def _extract_clean_json(self, text: str) -> dict:
        """استخراج JSON صلب ضد الهلوسات وعلامات الـ Markdown."""
        try:
            # إزالة علامات الماركداون الشائعة
            cleaned = re.sub(r"
            cleaned = re.sub(r"\s*```", "", cleaned).strip()
            
            # العثور على بداية ونهاية كائن JSON
            start_idx = cleaned.find('{')
            end_idx = cleaned.rfind('}')
            
            if start_idx == -1 or end_idx == -1:
                raise ValueError("لا يوجد كائن JSON في النص المولد.")
                
            json_str = cleaned[start_idx:end_idx+1]
            return json.loads(json_str)
        except Exception as e:
            logger.error(f"❌ فشل تحليل JSON. النص المرفوض: {text[:300]}...")
            raise e

    def _generate_chunk(self, prompt: str, system_prompt: str, context_name: str = "Chunk") -> dict:
        """
        توليد جزء واحد من السكريبت مع إعادة محاولة ذكية (Retry Mechanism).
        في حال فشل نموذج، يتم التبديل للنموذج الذي يليه فوراً.
        """
        max_attempts = len(self.adapters) + 1  # المحاولة بعدد المفاتيح المتاحة + 1
        
        for attempt in range(max_attempts):
            adapter, model = self._get_next_adapter()
            logger.info(f"🔄 [{context_name}] محاولة ({attempt+1}/{max_attempts}) عبر {model}...")
            try:
                response = adapter.generate(prompt, system_prompt, model)
                return self._extract_clean_json(response)
            except Exception as e:
                logger.warning(f"⚠️ فشل التوليد مع {model}. سيتم التبديل لمفتاح آخر. (السبب: {e})")
        
        raise RuntimeError(f"🚨 انهيار كامل: فشلت كافة النماذج في توليد {context_name}.")

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=15))
    def _fetch_ayahs(self, info: dict) -> list[VerifiedAyah]:
        """جلب الآيات القرآنية بدقة تامة من واجهة القرآن الكريم."""
        ayahs = []
        for n in range(info["start"], info["end"] + 1):
            url = f"https://api.qurancdn.com/api/qdc/verses/by_key/{info['surah']}:{n}?words=false&fields=text_uthmani"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            ayahs.append(VerifiedAyah(
                surah=info["surah"], 
                number=n, 
                text=data["verse"]["text_uthmani"],
                source="quran.com"
            ))
        return ayahs

    def generate(self, episode_num: int) -> EpisodeScript:
        info = CURRICULUM[episode_num]
        ayahs = self._fetch_ayahs(info)
        
        # ─── التوجيه السيكولوجي التربوي الصارم (Master System Prompt) ───
        sys_prompt = """أنت 'الجد أبو زياد'، عالم أزهري مصري، وحكاء حنون جداً للأطفال (5-8 سنوات).
[قواعد سيكولوجية وتربوية صارمة ومقدسة]:
1. الإيجابية المطلقة: اشرح الآيات من منظور الحب، الرحمة، الجمال، وعظمة الخالق.
2. ممنوع التخويف تماماً: يُحظر استخدام كلمات مثل (عذاب، نار، عقاب، غضب، جهنم، كفار). استبدلها بمفاهيم إيجابية (ربنا بيحبنا، بيحمينا، الجنة، السلام).
3. التوجيه البصري (visual_prompt): يجب أن يكون باللغة الإنجليزية، يبدأ بـ "Pixar/Disney 3D CGI, hyper-detailed", ويصف الموقف المذكور في الشرح لتوليد صورة مطابقة له.
4. الرد حصرياً بصيغة RAW JSON صالحة. لا تكتب أي كلمة خارج هيكل JSON."""

        # 1. توليد المقدمة
        intro_prompt = f"""قم بكتابة مقدمة دافئة ومشوقة لحلقة تتحدث عن سورة {info['name']}.
أجب بـ JSON فقط:
{{
  "title": "عنوان الحلقة (للسكريبت)",
  "youtube_title": "عنوان جذاب جداً ليوتيوب",
  "youtube_description": "وصف جذاب للحلقة ليوضع في يوتيوب",
  "intro_text": "نص عامي مصري راقي، ترحيب حار من الجد للأحفاد وتمهيد مبسط للسورة",
  "visual_prompt": "وصف إنجليزي 3D للجد والأطفال وهم يجلسون في غرفة دافئة للإضاءة"
}}"""
        intro_data = self._generate_chunk(intro_prompt, sys_prompt, context_name="المقدمة")

        # 2. توليد تفسير الآيات (Tafsir Chunking)
        ayah_scenes = []
        for i, ayah in enumerate(ayahs):
            ayah_prompt = f"""الآية القرآنية الكريمة: "{ayah.text}"
المطلوب: اشرح هذه الآية للطفل شرحاً مستفيضاً وعميقاً، واربطها بموقف يومي من حياته.
أجب بـ JSON فقط:
{{
  "intro_text": "جملة واحدة قصيرة جداً للتمهيد لسماع الآية (مثل: تعالوا نسمع الآية دي بتقول إيه...)",
  "explain_text": "شرح مستفيض ودافئ جداً للآية، يبدأ دائماً بعبارة 'صدق الله العظيم.'",
  "visual_prompt": "وصف إنجليزي 3D دقيق للموقف اليومي المذكور في الشرح"
}}"""
            a_data = self._generate_chunk(ayah_prompt, sys_prompt, context_name=f"الآية {ayah.number}")
            
            ayah_scenes.append(AyahScene(
                scene_id=10+i, 
                ayah=ayah, 
                duration_sec=40.0, # وقت كافٍ للتفسير المستفيض
                intro_text=a_data.get("intro_text", "نسمع الآية مع بعض..."),
                explain_text=a_data.get("explain_text", "صدق الله العظيم."),
                visual_prompt=a_data.get("visual_prompt", "Pixar 3D style, warm lighting.")
            ))

        # 3. توليد الخاتمة
        outro_prompt = """قم بكتابة خاتمة دافئة للحلقة مع دعاء جميل للأطفال ليرددوه قبل النوم.
أجب بـ JSON فقط:
{
  "narrator_text": "نص الخاتمة والدعاء باللهجة المصرية الراقية",
  "visual_prompt": "وصف إنجليزي 3D للجد وهو يغطي الأطفال ليناموا بسلام"
}"""
        outro_data = self._generate_chunk(outro_prompt, sys_prompt, context_name="الخاتمة")

        # 4. بناء الكائن النهائي وحفظه
        script = EpisodeScript(
            episode_number=episode_num, 
            surah_name=info["name"], 
            surah_number=info["surah"],
            title=intro_data.get("title", f"سورة {info['name']}"), 
            youtube_title=intro_data.get("youtube_title", f"تفسير سورة {info['name']} للأطفال"),
            youtube_description=intro_data.get("youtube_description", "حلقة جديدة من قِيمة."), 
            youtube_tags=["قرآن_للأطفال", "تفسير_ميسر", "الجد_أبو_زياد"], 
            total_duration_sec=300.0,
            
            intro_scene=NarratorScene(
                scene_id=1, scene_type=SceneType.INTRO, duration_sec=30.0,
                narrator_text=intro_data.get("intro_text", "أهلاً بكم يا أحبائي."), 
                visual_prompt=intro_data.get("visual_prompt", "Pixar 3D warm study room")
            ),
            
            ayah_scenes=ayah_scenes,
            
            outro_scene=NarratorScene(
                scene_id=99, scene_type=SceneType.OUTRO, duration_sec=30.0,
                narrator_text=outro_data.get("narrator_text", "تصبحون على خير وفي حفظ الله."), 
                visual_prompt=outro_data.get("visual_prompt", "Pixar 3D kids sleeping peacefully")
            )
        )
        
        # حفظ السكريبت كـ JSON للاستخدام اللاحق
        save_path = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        
        logger.info(f"✅ اكتمل توليد سكريبت الحلقة {episode_num} بنجاح تام.")
        return script

    def load_from_disk(self, episode_num: int) -> Optional[EpisodeScript]:
        """محاولة استرجاع السكريبت من القرص إذا كان مولداً مسبقاً."""
        p = Paths.SCRIPT_DIR / f"episode_{episode_num:03d}.json"
        if p.exists():
            try:
                return EpisodeScript.model_validate(json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:
                logger.error(f"❌ فشل قراءة السكريبت من القرص: {e}")
        return None