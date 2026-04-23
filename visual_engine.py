"""
visual_engine.py — VALUE / QEEMA v2
محرك الصور: ذكاء الإنفوجرافيك (Smart Infographic Engine)
• توليد حصري لرسوم الإنفوجرافيك المسطحة للأطفال
• خالية من الصور الثابتة، وتعتمد على محاولات بديلة ذكية
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import APIKeys, Paths, VisualConfig
from models import AyahScene, EpisodeScript, NarratorScene

logger = logging.getLogger(__name__)

class VisualEngine:
    API = "https://cloud.leonardo.ai/api/rest/v1"
    
    # 👈 الأساس الصارم للإنفوجرافيك
    BASE_STYLE = (
        "flat vector graphic, 2d educational infographic style for kids, "
        "clean solid pastel background, modern UI elements, simple shapes, "
        "no text, no letters, no gradients, highly aesthetic, minimalist"
    )

    def __init__(self):
        if not APIKeys.LEONARDO:
            raise ValueError("❌ مفتاح LEONARDO_API_KEY مفقود")
        self.headers = {
            "authorization": f"Bearer {APIKeys.LEONARDO}",
            "content-type": "application/json",
        }
        Paths.ensure_all()

    def _build_infographic_prompt(self, base_concept: str, scene_type: str) -> str:
        """
        حقن متغير وذكي لأسلوب الإنفوجرافيك بناءً على نوع المشهد،
        ليمنع التكرار والرتابة ويضمن التنوع في كل فيديو.
        """
        # إذا كان المشهد راوي (مقدمة/خاتمة/شرح)، نركز على الأيقونات التوضيحية
        if scene_type in ["intro", "outro", "narrator"]:
            modifiers = "isometric vector illustration, bright cheerful colors, educational concept art, "
        # إذا كان قرآن (آية)، نركز على الرسوم البيانية الروحانية الهادئة
        else:
            modifiers = "geometric Islamic patterns vector, calm warm color palette, symbolic minimalist art, "

        # دمج الفكرة الأساسية + المتغيرات + الأساس الصارم
        full_prompt = f"{base_concept}, {modifiers} {self.BASE_STYLE}"
        return full_prompt.replace(", ,", ",").strip()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=15))
    def _request(self, prompt: str) -> str:
        """إرسال الطلب مع إجبار الذكاء الاصطناعي على الرسم (ILLUSTRATION)"""
        payload = {
            "prompt": prompt,
            "negative_prompt": VisualConfig.NEGATIVE_PROMPT,
            "modelId": VisualConfig.MODEL_ANIME, # يفضل استخدام موديل يدعم الرسوميات
            "num_images": VisualConfig.NUM_IMAGES,
            "width": VisualConfig.WIDTH,
            "height": VisualConfig.HEIGHT,
            "guidance_scale": VisualConfig.GUIDANCE_SCALE,
            "num_inference_steps": VisualConfig.STEPS,
            "presetStyle": "ILLUSTRATION" # 👈 إجبار على الرسم المسطح
        }

        r = requests.post(f"{self.API}/generations", headers=self.headers, json=payload, timeout=30)
        if r.status_code != 200:
            logger.error(f"Leonardo API Request Error: {r.text}")
        r.raise_for_status()
        
        return r.json()["sdGenerationJob"]["generationId"]

    @retry(stop=stop_after_attempt(12), wait=wait_exponential(min=4, max=15))
    def _poll(self, gen_id: str) -> str:
        r = requests.get(f"{self.API}/generations/{gen_id}", headers=self.headers, timeout=15)
        r.raise_for_status()
        data = r.json().get("generations_by_pk", {})
        
        status = data.get("status")
        if status == "COMPLETE":
            return data["generated_images"][0]["url"]
        if status == "FAILED":
            raise RuntimeError("Generation marked as FAILED by Leonardo.")
        raise Exception("Still processing...")

    def _download(self, url: str, path: str) -> str:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(r.content)
        return path

    def generate_scene_image(
        self, original_prompt: str, output_path: str, scene_type: str = "narrator"
    ) -> str:
        """
        توليد ذكي: إذا فشل الوصف المعقد، يحاول المحرك تبسيط الوصف 
        لتوليد صورة حقيقية بدلاً من الاستسلام لصور ثابتة.
        """
        primary_prompt = self._build_infographic_prompt(original_prompt, scene_type)
        logger.info(f"📊 توليد إنفوجرافيك: {original_prompt[:45]}...")
        
        try:
            gen_id = self._request(primary_prompt)
            time.sleep(4)
            url = self._poll(gen_id)
            return self._download(url, output_path)
            
        except Exception as e:
            logger.warning(f"⚠️ فشل الطلب الأساسي للإنفوجرافيك. السبب: {e}")
            logger.info("🔄 تفعيل المحاولة الذكية بوصف بديل مبسط...")
            
            # 👈 المحاولة الذكية (Smart Fallback): إرسال وصف مبسط جداً لتجاوز أخطاء الكلمات المحظورة أو التعقيد
            safe_prompt = f"abstract minimalist islamic vector art, flat colors, {self.BASE_STYLE}"
            
            try:
                gen_id = self._request(safe_prompt)
                time.sleep(4)
                url = self._poll(gen_id)
                logger.info("✅ نجحت المحاولة الذكية للإنفوجرافيك.")
                return self._download(url, output_path)
            except Exception as final_e:
                logger.error(f"❌ فشلت جميع محاولات التوليد. النظام يتطلب التدخل: {final_e}")
                raise RuntimeError(f"فشل ذريع في توليد الصورة للمسار: {output_path}")

    def generate_episode_visuals(self, script: EpisodeScript, ep_dir: str) -> None:
        vis_dir = Path(ep_dir) / "visuals"
        vis_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"📈 بدء معالجة الإنفوجرافيك للحلقة {script.episode_number}")

        p_intro = str(vis_dir / "intro.png")
        self.generate_scene_image(script.intro_scene.visual_prompt, p_intro, "intro")
        script.intro_scene.image_path = p_intro
        time.sleep(2)

        for sc in script.ayah_scenes:
            p_ayah = str(vis_dir / f"ayah_{sc.scene_id:03d}.png")
            self.generate_scene_image(sc.visual_prompt, p_ayah, "ayah")
            sc.image_path = p_ayah
            time.sleep(2)

        for sc in script.mid_scenes:
            p_mid = str(vis_dir / f"mid_{sc.scene_id:03d}.png")
            self.generate_scene_image(sc.visual_prompt, p_mid, "narrator")
            sc.image_path = p_mid
            time.sleep(2)

        p_outro = str(vis_dir / "outro.png")
        self.generate_scene_image(script.outro_scene.visual_prompt, p_outro, "outro")
        script.outro_scene.image_path = p_outro

        logger.info("✅ اكتمل توليد جميع الإنفوجرافيكس بنجاح")
