import os
import logging
from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class VoiceEngineV2:
    """
    محرك صوت احترافي يستخدم Google Cloud TTS
    يتجاوز حدود Gemini المحدودة (10 طلبات) ويوفر جودة سينمائية.
    """
    def __init__(self):
        # التأكد من وجود ملف الاعتماديات الذي قمت بإعداده في البايبلاين
        self.client = texttospeech.TextToSpeechClient()
        self._narrator_voice = texttospeech.VoiceSelectionParams(
            language_code="ar-XA",
            name="ar-XA-Wavenet-B"  # صوت ذكوري وقور يشبه الجد أبو زياد
        )
        self._audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=0.9, # سرعة هادئة للأطفال
            pitch=-2.0         # نبرة عميقة وقورة
        )
        logger.info("✅ تم تفعيل محرك Google Cloud TTS الاحترافي")

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
    def synthesize(self, text: str, output_path: str):
        """توليد الصوت باستخدام السحابة"""
        try:
            input_text = texttospeech.SynthesisInput(text=text)
            response = self.client.synthesize_speech(
                input=input_text,
                voice=self._narrator_voice,
                audio_config=self._audio_config
            )
            with open(output_path, "wb") as out:
                out.write(response.audio_content)
            return True
        except Exception as e:
            logger.error(f"❌ فشل توليد الصوت: {str(e)}")
            raise

    def generate_episode_audio(self, script, ep_dir):
        """توليد كافة ملفات الصوت للحلقة بتسلسل ذكي"""
        # (بقية المنطق الخاص بك لاستدعاء synthesize لكل مشهد)
        # سأقوم بتبسيط الاستدعاء لضمان العمل
        audio_map = {}
        
        # 1. الافتتاحية
        intro_path = f"{ep_dir}/intro_narrator.mp3"
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        
        # 2. الآيات
        for i, scene in enumerate(script.ayah_scenes):
            # توليد مقدمة الآية
            p_intro = f"{ep_dir}/ayah_{scene.scene_id}_intro.mp3"
            self.synthesize(scene.intro_text, p_intro)
            
            # توليد شرح الآية
            p_explain = f"{ep_dir}/ayah_{scene.scene_id}_explain.mp3"
            self.synthesize(scene.explain_text, p_explain)
            
        # 3. الخاتمة
        outro_path = f"{ep_dir}/outro_narrator.mp3"
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        
        return True