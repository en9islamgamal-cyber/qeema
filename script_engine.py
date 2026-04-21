"""
Script & Director Engine - QEEMA Pipeline
Uses Google GenAI, with automatic fallback to Anthropic Claude upon failure.
"""

import os
import json
import time
import logging
import requests
from typing import Dict, Any
from google import genai
from google.genai import types

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

def _clean_json_text(text: str) -> str:
    """Helper to clean markdown formatting from LLM responses."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()

# =============================================================================
# 2. API CLIENTS
# =============================================================================

def _call_gemini(user_prompt: str, api_key: str) -> dict:
    log.info("🤖 Attempting to use Gemini 2.5 Flash...")
    client = genai.Client(api_key=api_key)
    
    # Retry logic for Gemini (e.g. 503 errors)
    last_err = None
    for attempt in range(1, 4):
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
            script_data = json.loads(_clean_json_text(response.text))
            if "scenes" not in script_data:
                raise ValueError("JSON does not contain 'scenes'.")
            log.info("✅ Gemini succeeded!")
            return script_data
        except Exception as e:
            last_err = e
            log.warning(f"⚠️ Gemini attempt {attempt} failed: {e}")
            time.sleep(3 * attempt)
            
    raise RuntimeError(f"Gemini exhausted: {last_err}")

def _call_claude(user_prompt: str, api_key: str) -> dict:
    log.info("🟠 Falling back to Claude Sonnet...")
    api_url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    
    # Combine system prompt and user prompt for Claude
    full_prompt = f"{DIRECTOR_PROMPT}\n\n{user_prompt}\n\nPlease respond ONLY with valid JSON."
    
    payload = {
        "model": "claude-3-5-sonnet-20241022", # Updated to latest standard model string
        "max_tokens": 4096,
        "temperature": 0.4,
        "messages": [{"role": "user", "content": full_prompt}],
    }

    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.post(api_url, json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            data = r.json()
            content = data.get("content") or []
            if not content:
                raise ValueError("Claude response without content")
            
            raw_text = content[0].get("text", "")
            script_data = json.loads(_clean_json_text(raw_text))
            
            if "scenes" not in script_data:
                raise ValueError("JSON does not contain 'scenes'.")
                
            log.info("✅ Claude succeeded!")
            return script_data
        except Exception as e:
            last_err = e
            log.warning(f"⚠️ Claude attempt {attempt} failed: {e}")
            time.sleep(3 * attempt)
            
    raise RuntimeError(f"Claude exhausted: {last_err}")

# =============================================================================
# 3. MAIN SCRIPT GENERATOR
# =============================================================================

def generate_cinematic_script(surah_name: str, ayah_start: int, ayah_end: int, gemini_key: str) -> Dict[str, Any]:
    """
    Generates the script using Gemini, falling back to Claude if Anthropic key is available.
    """
    user_prompt = f"قم بإعداد سيناريو الإخراج وتفسير سورة {surah_name}، الآيات من {ayah_start} إلى {ayah_end}."
    log.info(f"🎬 يكتب الآن سيناريو سورة {surah_name} (الآيات {ayah_start}-{ayah_end})...")
    
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if gemini_key:
        try:
            return _call_gemini(user_prompt, gemini_key)
        except Exception as gemini_err:
            log.warning(f"⚠️ Gemini failed completely: {gemini_err}")
    
    if anthropic_key:
        try:
            return _call_claude(user_prompt, anthropic_key)
        except Exception as claude_err:
            log.error(f"❌ Claude also failed: {claude_err}")
            raise RuntimeError("❌ فشل توليد السيناريو من Gemini و Claude")
    else:
        raise RuntimeError("❌ فشل Gemini ولا يوجد ANTHROPIC_API_KEY كبديل")
