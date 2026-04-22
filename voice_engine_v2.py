import os
import logging
from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class VoiceEngine:  # 👈 تم تغيير الاسم ليتطابق مع طلب الـ Orchestrator
    """
    محرك صوت احترافي يستخدم Google Cloud TTS
    يتجاوز حدود Gemini المحدودة ويوفر جودة سينمائية للأطفال.
    """
    def __init__(self):
        # التأكد من وجود الاعتماديات
        try:
            self.client = texttospeech.TextToSpeechClient()
            self._narrator_voice = texttospeech.VoiceSelectionParams(
                language_code="ar-XA",
                name="ar-XA-Wavenet-B"  # صوت ذكوري وقور (الجد أبو زياد)
            )
            self._audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=0.85, # سرعة هادئة جداً للأطفال
                pitch=-2.0          # نبرة عميقة وحنونة
            )
            logger.info("✅ تم تفعيل محرك Google Cloud TTS بنجاح")
        except Exception as e:
            logger.error(f"❌ فشل تهيئة GCP TTS: {str(e)}")
            raise

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=15))
    def synthesize(self, text: str, output_path: str):
        """توليد ملف صوتي واحد من نص"""
        if not text:
            return False
        
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
            logger.error(f"❌ فشل توليد الصوت للنص: {text[:30]}... الخطأ: {str(e)}")
            raise

    def generate_episode_audio(self, script, ep_dir):
        """المحرك الرئيسي لتوليد كافة أصوات الحلقة"""
        logger.info(f"🎙️ بدء توليد أصوات الحلقة {script.episode_number}...")
        
        # 1. صوت الافتتاحية
        intro_path = os.path.join(ep_dir, "intro_narrator.mp3")
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        
        # 2. أصوات مشاهد الآيات
        for scene in script.ayah_scenes:
            # صوت التمهيد للآية
            p_intro = os.path.join(ep_dir, f"ayah_{scene.scene_id}_intro.mp3")
            self.synthesize(scene.intro_text, p_intro)
            
            # صوت شرح الآية
            p_explain = os.path.join(ep_dir, f"ayah_{scene.scene_id}_explain.mp3")
            self.synthesize(scene.explain_text, p_explain)
            
        # 3. صوت الخاتمة
        outro_path = os.path.join(ep_dir, "outro_narrator.mp3")
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        
        logger.info("✅ تم توليد كافة الملفات الصوتية بنجاح عبر Google Cloud")
        return True