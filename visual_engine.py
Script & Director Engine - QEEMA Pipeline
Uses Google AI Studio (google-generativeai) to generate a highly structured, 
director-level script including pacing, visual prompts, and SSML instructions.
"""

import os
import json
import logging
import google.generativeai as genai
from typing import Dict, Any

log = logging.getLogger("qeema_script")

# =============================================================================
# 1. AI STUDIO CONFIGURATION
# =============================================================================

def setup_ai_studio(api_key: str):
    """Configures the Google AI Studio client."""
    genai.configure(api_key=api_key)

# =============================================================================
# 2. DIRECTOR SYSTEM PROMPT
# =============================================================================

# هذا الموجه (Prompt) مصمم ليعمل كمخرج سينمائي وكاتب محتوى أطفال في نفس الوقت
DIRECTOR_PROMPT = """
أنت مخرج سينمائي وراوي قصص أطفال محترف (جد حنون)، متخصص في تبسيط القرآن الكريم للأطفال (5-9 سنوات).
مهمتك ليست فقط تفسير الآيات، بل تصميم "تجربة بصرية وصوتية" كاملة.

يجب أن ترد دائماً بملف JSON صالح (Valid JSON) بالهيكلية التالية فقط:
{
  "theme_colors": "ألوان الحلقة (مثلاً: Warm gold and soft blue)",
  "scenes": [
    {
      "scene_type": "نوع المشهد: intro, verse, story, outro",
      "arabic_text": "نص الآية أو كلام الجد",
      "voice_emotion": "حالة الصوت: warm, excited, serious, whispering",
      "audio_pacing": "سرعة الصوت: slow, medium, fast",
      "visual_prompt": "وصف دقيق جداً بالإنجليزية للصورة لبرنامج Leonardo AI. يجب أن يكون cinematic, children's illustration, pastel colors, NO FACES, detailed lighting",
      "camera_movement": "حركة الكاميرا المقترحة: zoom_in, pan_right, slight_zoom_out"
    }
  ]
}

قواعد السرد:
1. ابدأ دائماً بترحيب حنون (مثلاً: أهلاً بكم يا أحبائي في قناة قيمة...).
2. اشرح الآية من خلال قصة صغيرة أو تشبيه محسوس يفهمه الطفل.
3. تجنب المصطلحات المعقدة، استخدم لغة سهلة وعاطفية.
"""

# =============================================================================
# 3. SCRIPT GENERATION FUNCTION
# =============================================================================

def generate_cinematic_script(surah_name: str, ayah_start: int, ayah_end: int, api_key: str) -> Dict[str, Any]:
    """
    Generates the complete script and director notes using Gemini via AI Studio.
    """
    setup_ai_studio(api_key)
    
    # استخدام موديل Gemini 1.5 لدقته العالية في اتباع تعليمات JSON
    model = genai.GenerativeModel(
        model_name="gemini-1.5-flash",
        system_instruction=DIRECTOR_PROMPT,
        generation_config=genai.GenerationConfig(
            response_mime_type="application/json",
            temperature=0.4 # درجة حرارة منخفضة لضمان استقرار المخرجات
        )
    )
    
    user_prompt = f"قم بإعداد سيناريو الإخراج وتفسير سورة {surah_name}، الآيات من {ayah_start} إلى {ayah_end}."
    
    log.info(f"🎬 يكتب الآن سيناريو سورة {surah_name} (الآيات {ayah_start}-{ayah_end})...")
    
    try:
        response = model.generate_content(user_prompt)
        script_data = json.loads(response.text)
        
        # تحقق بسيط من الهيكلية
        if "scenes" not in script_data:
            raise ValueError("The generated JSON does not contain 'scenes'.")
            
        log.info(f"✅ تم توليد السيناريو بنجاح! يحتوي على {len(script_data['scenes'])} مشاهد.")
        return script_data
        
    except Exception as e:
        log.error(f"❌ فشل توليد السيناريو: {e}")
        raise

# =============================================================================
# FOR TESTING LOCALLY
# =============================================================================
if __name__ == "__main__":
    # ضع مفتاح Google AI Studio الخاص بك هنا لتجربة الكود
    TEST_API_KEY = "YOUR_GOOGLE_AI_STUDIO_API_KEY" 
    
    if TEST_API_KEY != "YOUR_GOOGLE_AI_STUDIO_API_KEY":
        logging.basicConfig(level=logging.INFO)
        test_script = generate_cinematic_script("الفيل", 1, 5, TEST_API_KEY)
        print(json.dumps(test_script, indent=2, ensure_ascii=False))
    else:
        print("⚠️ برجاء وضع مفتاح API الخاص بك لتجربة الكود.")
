"""
Script & Director Engine - QEEMA Pipeline
Uses Google AI Studio (google-generativeai) to generate a highly structured, 
director-level script including pacing, visual prompts, and SSML instructions.
"""

import os
import json
import logging
import google.generativeai as genai
from typing import Dict, Any

log = logging.getLogger("qeema_script")

# =============================================================================
# 1. AI STUDIO CONFIGURATION
# =============================================================================

def setup_ai_studio(api_key: str):
    """Configures the Google AI Studio client."""
    genai.configure(api_key=api_key)

# =============================================================================
# 2. DIRECTOR SYSTEM PROMPT
# =============================================================================

# هذا الموجه (Prompt) مصمم ليعمل كمخرج سينمائي وكاتب محتوى أطفال في نفس الوقت
DIRECTOR_PROMPT = """
أنت مخرج سينمائي وراوي قصص أطفال محترف (جد حنون)، متخصص في تبسيط القرآن الكريم للأطفال (5-9 سنوات).
مهمتك ليست فقط تفسير الآيات، بل تصميم "تجربة بصرية وصوتية" كاملة.

يجب أن ترد دائماً بملف JSON صالح (Valid JSON) بالهيكلية التالية فقط:
{
  "theme_colors": "ألوان الحلقة (مثلاً: Warm gold and soft blue)",
  "scenes": [
    {
      "scene_type": "نوع المشهد: intro, verse, story, outro",
      "arabic_text": "نص الآية أو كلام الجد",
      "voice_emotion": "حالة الصوت: warm, excited, serious, whispering",
      "audio_pacing": "سرعة الصوت: slow, medium, fast",
      "visual_prompt": "وصف دقيق جداً بالإنجليزية للصورة لبرنامج Leonardo AI. يجب أن يكون cinematic, children's illustration, pastel colors, NO FACES, detailed lighting",
      "camera_movement": "حركة الكاميرا المقترحة: zoom_in, pan_right, slight_zoom_out"
    }
  ]
}

قواعد السرد:
1. ابدأ دائماً بترحيب حنون (مثلاً: أهلاً بكم يا أحبائي في قناة قيمة...).
2. اشرح الآية من خلال قصة صغيرة أو تشبيه محسوس يفهمه الطفل.
3. تجنب المصطلحات المعقدة، استخدم لغة سهلة وعاطفية.
"""

# =============================================================================
# 3. SCRIPT GENERATION FUNCTION
# =============================================================================

def generate_cinematic_script(surah_name: str, ayah_start: int, ayah_end: int, api_key: str) -> Dict[str, Any]:
    """
    Generates the complete script and director notes using Gemini via AI Studio.
    """
    setup_ai_studio(api_key)
    
    # استخدام موديل Gemini 1.5 لدقته العالية في اتباع تعليمات JSON
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=DIRECTOR_PROMPT,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.4 # درجة حرارة منخفضة لضمان استقرار المخرجات
        )
    )
    
    user_prompt = f"قم بإعداد سيناريو الإخراج وتفسير سورة {surah_name}، الآيات من {ayah_start} إلى {ayah_end}."
    
    log.info(f"🎬 يكتب الآن سيناريو سورة {surah_name} (الآيات {ayah_start}-{ayah_end})...")
    
    try:
        response = model.generate_content(user_prompt)
        script_data = json.loads(response.text)
        
        # تحقق بسيط من الهيكلية
        if "scenes" not in script_data:
            raise ValueError("The generated JSON does not contain 'scenes'.")
            
        log.info(f"✅ تم توليد السيناريو بنجاح! يحتوي على {len(script_data['scenes'])} مشاهد.")
        return script_data
        
    except Exception as e:
        log.error(f"❌ فشل توليد السيناريو: {e}")
        raise

# =============================================================================
# FOR TESTING LOCALLY
# =============================================================================
if __name__ == "__main__":
    # ضع مفتاح Google AI Studio الخاص بك هنا لتجربة الكود
    TEST_API_KEY = "YOUR_GOOGLE_AI_STUDIO_API_KEY" 
    
    if TEST_API_KEY != "YOUR_GOOGLE_AI_STUDIO_API_KEY":
        logging.basicConfig(level=logging.INFO)
        test_script = generate_cinematic_script("الفيل", 1, 5, TEST_API_KEY)
        print(json.dumps(test_script, indent=2, ensure_ascii=False))
    else:
        print("⚠️ برجاء وضع مفتاح API الخاص بك لتجربة الكود.")
