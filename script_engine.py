"""
Script & Director Engine - QEEMA Pipeline
Uses Google's NEW AI SDK (google-genai) to generate a highly structured script.
"""

import os
import json
import logging
from google import genai
from google.genai import types
from typing import Dict, Any

log = logging.getLogger("qeema_script")

# =============================================================================
# 1. DIRECTOR SYSTEM PROMPT
# =============================================================================

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
# 2. SCRIPT GENERATION FUNCTION
# =============================================================================

def generate_cinematic_script(surah_name: str, ayah_start: int, ayah_end: int, api_key: str) -> Dict[str, Any]:
    """
    Generates the script using the new google-genai library.
    """
    client = genai.Client(api_key=api_key)
    user_prompt = f"قم بإعداد سيناريو الإخراج وتفسير سورة {surah_name}، الآيات من {ayah_start} إلى {ayah_end}."
    
    log.info(f"🎬 يكتب الآن سيناريو سورة {surah_name} (الآيات {ayah_start}-{ayah_end})...")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.4,
                system_instruction=DIRECTOR_PROMPT,
            )
        )
        
        script_data = json.loads(response.text)
        
        if "scenes" not in script_data:
            raise ValueError("The generated JSON does not contain 'scenes'.")
            
        log.info(f"✅ تم توليد السيناريو بنجاح! يحتوي على {len(script_data['scenes'])} مشاهد.")
        return script_data
        
    except Exception as e:
        log.error(f"❌ فشل توليد السيناريو: {e}")
        raise
