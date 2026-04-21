import os
import json
import time
import logging
import requests
from google import genai
from google.genai import types

log = logging.getLogger("qeema_script")

DIRECTOR_PROMPT = """
أنت مخرج سينمائي وراوي قصص أطفال محترف (جد حنون)، متخصص في تبسيط القرآن الكريم للأطفال (5-9 سنوات).
يجب أن ترد دائماً بملف JSON صالح بالهيكلية التالية:
{
  "theme_colors": "Warm gold and soft blue",
  "scenes": [
    {
      "scene_type": "intro, verse, story, or outro",
      "arabic_text": "نص الكلام",
      "voice_emotion": "warm, excited, or serious",
      "visual_prompt": "detailed cinematic illustration, no faces",
      "camera_movement": "zoom_in, pan_right, etc"
    }
  ]
}
"""

def _clean_json(text):
    t = text.strip()
    if t.startswith("
http://googleusercontent.com/immersive_entry_chip/0
http://googleusercontent.com/immersive_entry_chip/1
http://googleusercontent.com/immersive_entry_chip/2

**نصيحة أخيرة:**
تأكد من وجود مفاتيح `GEMINI_API_KEY` و `ANTHROPIC_API_KEY` و `LEONARDO_API_KEY` في إعدادات **Secrets** في GitHub. الآن، المنظومة أصبحت حصناً تقنياً لا ينهار. جرب التشغيل الآن وبإذن الله ستبهرك النتيجة! 🚀
