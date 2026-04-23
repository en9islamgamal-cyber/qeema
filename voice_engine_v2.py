import os  # تم تأكيد كتابتها بحروف صغيرة
import re
import logging
from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class VoiceEngine:
    """
    محرك صوت احترافي يستخدم Google Cloud TTS مع دعم SSML
    • تم ضبطه لتجاهل تشكيل أواخر الكلمات (الوقوف على سكون)
    • إضافة وقفات تنفس طبيعية كالبشر
    • سرعة ونبرة مضبوطة لشخصية الجد أبو زياد
    """
    def __init__(self):
        try:
            self.client = texttospeech.TextToSpeechClient()

            self._narrator_voice = texttospeech.VoiceSelectionParams(
                language_code="ar-XA",
                name="ar-XA-Wavenet-B"
            )

            # تم ضبط السرعة لتكون طبيعية أكثر مع وقفات التنفس
            self._audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=0.90, 
                pitch=-2.0          
            )
            logger.info("✅ تم تفعيل محرك Google Cloud TTS (بدعم SSML) بنجاح")
        except Exception as e:
            logger.error(f"❌ فشل تهيئة GCP TTS: {str(e)}")
            raise

    def _prepare_ssml(self, text: str) -> str:
        """
        معالجة النص وتحويله إلى SSML لنطق بشري طبيعي
        """
        # 1. إزالة التشكيل (الحركات) من أواخر الكلمات قبل المسافات أو علامات الترقيم
        # النطاق \u064B-\u0652 يمثل جميع حركات التشكيل العربية
        text = re.sub(r'[\u064B-\u0652]+(?=\s|$|[،.؟!])', '', text)

        # 2. برمجة وقفات التنفس (Breaks)
        text = text.replace('،', '<break time="400ms"/>')
        text = text.replace('.', '<break time="800ms"/>')
        text = text.replace('؟', '<break time="800ms"/>')
        text = text.replace('!', '<break time="600ms"/>')

        # 3. تغليف النص ليفهمه محرك جوجل
        return f"<speak>{text}</speak>"

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=15))
    def synthesize(self, text: str, output_path: str):
        """توليد ملف صوتي MP3 باستخدام SSML"""
        if not text or len(text.strip()) < 2:
            return False

        # معالجة النص قبل الإرسال
        ssml_text = self._prepare_ssml(text)

        try:
            # التغيير الجوهري هنا: نمرر ssml بدلاً من text
            input_text = texttospeech.SynthesisInput(ssml=ssml_text)
            response = self.client.synthesize_speech(
                input=input_text,
                voice=self._narrator_voice,
                audio_config=self._audio_config
            )

            with open(output_path, "wb") as out:
                out.write(response.audio_content)

            import time
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في توليد الصوت للنص: {text[:30]}... | الخطأ: {str(e)}")
            raise

    def generate_episode_audio(self, script, ep_dir) -> dict:
        """المحرك الرئيسي لتوليد كافة ملفات الصوت للحلقة"""
        logger.info(f"🎙️ بدء إنتاج أصوات الحلقة {script.episode_number}...")

        audio_map = {}

        # 1. صوت مقدمة الجد
        intro_path = os.path.join(ep_dir, "intro_narrator.mp3")
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        audio_map["intro"] = intro_path
        logger.info("✅ مقدمة الراوي جاهزة")

        # 2. أصوات مشاهد الآيات
        for i, scene in enumerate(script.ayah_scenes):
            p_intro = os.path.join(ep_dir, f"ayah_{scene.scene_id}_intro.mp3")
            self.synthesize(scene.intro_text, p_intro)
            audio_map[f"ayah_{scene.scene_id}_intro"] = p_intro

            p_explain = os.path.join(ep_dir, f"ayah_{scene.scene_id}_explain.mp3")
            self.synthesize(scene.explain_text, p_explain)
            audio_map[f"ayah_{scene.scene_id}_explain"] = p_explain

            logger.info(f"✅ أصوات الآية {scene.ayah.number} جاهزة")

        # 3. صوت الخاتمة
        outro_path = os.path.join(ep_dir, "outro_narrator.mp3")
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        audio_map["outro"] = outro_path
        logger.info("✅ خاتمة الراوي جاهزة")

        logger.info("🎉 اكتمل إنتاج كافة الملفات الصوتية بنجاح!")
        return audio_map
