"""
Visual Engine - QEEMA Pipeline
Handles Leonardo AI with robust retry logic and exponential backoff.
"""
import time
import requests
import logging
from pathlib import Path

log = logging.getLogger("qeema_visual")

LEONARDO_MODEL_ID = "6b645e3a-d64f-4341-a6d8-7a3690fbf042" 
IMAGE_W, IMAGE_H = 1472, 832

def generate_professional_image(prompt: str, output_path: Path, api_key: str, theme_colors: str) -> None:
    if not api_key: raise ValueError("❌ مفتاح Leonardo API مفقود!")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "accept": "application/json"}
    engineered_prompt = (
        f"Cinematic masterpiece, children's Islamic storybook illustration. "
        f"Color palette: {theme_colors}. Highly detailed, magical lighting, soft shadows, peaceful atmosphere. "
        f"NO FACES, no human features visible. Action: {prompt}"
    )
    body = {
        "prompt": engineered_prompt, "modelId": LEONARDO_MODEL_ID,
        "width": IMAGE_W, "height": IMAGE_H, "num_images": 1,
        "alchemy": True, "presetStyle": "CINEMATIC"
    }

    log.info(f"🎨 Sending prompt to Leonardo AI...")

    # 1. ذكاء المحاولة التلقائية عند طلب الصورة
    generation_id = None
    last_err = None
    for attempt in range(1, 4):
        try:
            response = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=body, headers=headers, timeout=60)
            if response.status_code in [429, 503]:
                log.warning(f"⚠️ سيرفرات Leonardo مزدحمة. محاولة {attempt}/3 بعد قليل...")
                time.sleep(5 * attempt)
                continue
                
            response.raise_for_status()
            generation_id = response.json().get("sdGenerationJob", {}).get("generationId")
            if generation_id: break
        except Exception as e:
            last_err = e
            log.warning(f"⚠️ خطأ في الاتصال بـ Leonardo (محاولة {attempt}): {e}")
            time.sleep(4 * attempt)
            
    if not generation_id:
        raise RuntimeError(f"❌ فشل توليد الصورة بعد عدة محاولات: {last_err}")

    # 2. ذكاء المتابعة (Polling)
    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}"
    for attempt in range(20):
        time.sleep(8)
        try:
            poll_res = requests.get(poll_url, headers=headers, timeout=30)
            poll_res.raise_for_status()
            job_data = poll_res.json().get("generations_by_pk", {}) or {}
            status = job_data.get("status")
            
            if status == "COMPLETE":
                images = job_data.get("generated_images") or []
                if images:
                    _download_image(images[0]["url"], output_path)
                    return
            elif status == "FAILED":
                raise RuntimeError("❌ فشل Leonardo في توليد الصورة داخلياً.")
        except Exception as e:
            log.warning(f"⚠️ خطأ أثناء التحقق من حالة الصورة: {e}")

    raise TimeoutError("❌ انتهى وقت الانتظار ولم يتم توليد الصورة.")

def _download_image(url: str, output_path: Path) -> None:
    for attempt in range(1, 4):
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(response.content)
            log.info(f"✅ تم حفظ الصورة بنجاح!")
            return
        except Exception as e:
            log.warning(f"⚠️ فشل تحميل الصورة، إعادة المحاولة {attempt}: {e}")
            time.sleep(2)
    raise RuntimeError("❌ فشل تحميل الصورة النهائية.")
