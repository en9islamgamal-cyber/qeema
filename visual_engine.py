"""
Visual Engine - QEEMA Pipeline
Handles Leonardo AI API integration with strict style consistency and polling mechanisms.
"""

import time
import requests
import logging
from pathlib import Path

log = logging.getLogger("qeema_visual")

# =============================================================================
# 1. LEONARDO AI CONFIGURATION
# =============================================================================

# نستخدم موديل Phoenix من Leonardo أو أي موديل مخصص للرسوم السينمائية (Cinematic)
LEONARDO_MODEL_ID = "6b645e3a-d64f-4341-a6d8-7a3690fbf042" 
IMAGE_W, IMAGE_H = 1472, 832 # أبعاد سينمائية ممتازة

def generate_professional_image(prompt: str, output_path: Path, api_key: str, theme_colors: str) -> None:
    """
    Generates a high-quality, style-consistent image using Leonardo AI.
    """
    if not api_key:
        raise ValueError("❌ مفتاح Leonardo API مفقود!")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }

    # حقن سيكولوجية الألوان والأسلوب الثابت برمجياً لضمان التناسق
    engineered_prompt = (
        f"Cinematic masterpiece, children's Islamic storybook illustration. "
        f"Color palette: {theme_colors}. "
        f"Highly detailed, magical lighting, soft shadows, peaceful atmosphere. "
        f"NO FACES, no human features visible. "
        f"Action: {prompt}"
    )

    body = {
        "prompt": engineered_prompt,
        "modelId": LEONARDO_MODEL_ID,
        "width": IMAGE_W,
        "height": IMAGE_H,
        "num_images": 1,
        "alchemy": True, # تفعيل محرك Alchemy لأعلى جودة
        "presetStyle": "CINEMATIC"
    }

    log.info(f"🎨 Sending prompt to Leonardo AI: {engineered_prompt[:50]}...")

    # 1. إرسال طلب التوليد
    try:
        response = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=body, headers=headers, timeout=60)
        response.raise_for_status()
        
        # استخراج معرف المهمة (Job ID)
        data = response.json()
        generation_id = data.get("sdGenerationJob", {}).get("generationId")
        
        if not generation_id:
            raise RuntimeError(f"❌ لم يتم العثور على Generation ID: {data}")
            
    except Exception as e:
        log.error(f"❌ فشل إرسال طلب الصورة: {e}")
        raise

    # 2. نظام الانتظار الذكي (Polling Mechanism)
    # توليد الصور يأخذ وقتاً، لذا يجب أن يسأل الكود السيرفر كل عدة ثوانٍ: "هل انتهيت؟"
    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}"
    
    for attempt in range(15): # نحاول 15 مرة كحد أقصى
        time.sleep(8) # ننتظر 8 ثوانٍ بين كل محاولة
        
        try:
            poll_res = requests.get(poll_url, headers=headers, timeout=30)
            poll_res.raise_for_status()
            job_data = poll_res.json().get("generations_by_pk", {}) or {}
            status = job_data.get("status")
            
            log.info(f"⏳ التحقق من الصورة (محاولة {attempt+1}/15): الحالة = {status}")
            
            if status == "COMPLETE":
                images = job_data.get("generated_images") or []
                if images:
                    image_url = images[0]["url"]
                    _download_image(image_url, output_path)
                    return
                else:
                    raise RuntimeError("❌ اكتملت المهمة لكن لا توجد صور!")
                    
            elif status == "FAILED":
                raise RuntimeError("❌ فشل Leonardo في توليد الصورة.")
                
        except Exception as e:
            log.warning(f"⚠️ خطأ أثناء التحقق من حالة الصورة: {e}")

    raise TimeoutError("❌ انتهى وقت الانتظار ولم يتم توليد الصورة.")

# =============================================================================
# 2. HELPER: DOWNLOAD IMAGE
# =============================================================================

def _download_image(url: str, output_path: Path) -> None:
    """Downloads the actual image file to the local directory."""
    log.info("⬇️ جاري تحميل الصورة النهائية...")
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    
    with open(output_path, "wb") as f:
        f.write(response.content)
    log.info(f"✅ تم حفظ الصورة بنجاح في: {output_path.name}")
