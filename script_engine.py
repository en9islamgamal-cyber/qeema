import os
import json
import time
import logging
import requests
from google import genai
from google.genai import types

log = logging.getLogger("qeema_script")

# تم التخلص من علامات التنصيص المتعددة الأسطر لحماية الكود من أخطاء النسخ عبر الموبايل
DIRECTOR_PROMPT = (
    "أنت مخرج سينمائي وراوي قصص أطفال محترف (جد حنون)، متخصص في تبسيط القرآن الكريم للأطفال (5-9 سنوات).\n"
    "يجب أن ترد دائماً بملف JSON صالح بالهيكلية التالية:\n"
    "{\n"
    '  "theme_colors": "Warm gold and soft blue",\n'
    '  "scenes": [\n'
    "    {\n"
    '      "scene_type": "intro, verse, story, or outro",\n'
    '      "arabic_text": "نص الكلام",\n'
    '      "voice_emotion": "warm, excited, or serious",\n'
    '      "visual_prompt": "detailed cinematic illustration, no faces",\n'
    '      "camera_movement": "zoom_in, pan_right, etc"\n'
    "    }\n"
    "  ]\n"
    "}"
)

def _clean_json(text):
    # استخدام نفس الخوارزمية المستقرة من كودك القديم
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n", 1)
        t = lines[1] if len(lines) > 1 else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()

def generate_cinematic_script(surah_name, ayah_start, ayah_end, gemini_key):
    user_prompt = "سيناريو سورة " + str(surah_name) + "، الآيات " + str(ayah_start) + " إلى " + str(ayah_end) + "."
    claude_key = os.environ.get("ANTHROPIC_API_KEY")

    # 1. محاولة مع Gemini
    if gemini_key:
        client = genai.Client(api_key=gemini_key)
        for attempt in range(1, 4):
            try:
                log.info("🤖 محاولة Gemini " + str(attempt) + "/3...")
                res = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        system_instruction=DIRECTOR_PROMPT
                    )
                )
                cleaned_text = _clean_json(res.text)
                return json.loads(cleaned_text)
            except Exception as e:
                log.warning("⚠️ فشل Gemini: " + str(e))
                time.sleep(5)

    # 2. التحويل التلقائي لـ Claude (نظام الحماية)
    if claude_key:
        log.info("🟠 التحويل التلقائي لـ Claude...")
        headers = {
            "x-api-key": claude_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": DIRECTOR_PROMPT + "\n\n" + user_prompt}]
        }
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
            r.raise_for_status()
            cleaned_text = _clean_json(r.json()['content'][0]['text'])
            return json.loads(cleaned_text)
        except Exception as e:
            log.error("❌ فشل Claude أيضاً: " + str(e))

    raise RuntimeError("❌ فشل إنتاج السيناريو من جميع المصادر.")
