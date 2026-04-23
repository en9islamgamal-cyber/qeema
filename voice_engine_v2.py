import os  # تم إصلاح حرف I الكبير
import logging
from google.cloud import texttospeech
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class VoiceEngine:
    """
    محرك صوت احترافي يستخدم Google Cloud TTS
    • يتجاوز حدود Gemini (10 طلبات يومياً)
    • جودة سينمائية للأطفال (Wavenet)
    • سرعة ونبرة مضبوطة لشخصية الجد أبو زياد
    """
    def __init__(self):
        try:
            # تهيئة عميل جوجل سحابياً (يعتمد على الملف الموجود في /tmp/gcp_sa.json)
            self.client = texttospeech.TextToSpeechClient()

            # إعدادات صوت "الجد أبو زياد"
            self._narrator_voice = texttospeech.VoiceSelectionParams(
                language_code="ar-XA",
                name="ar-XA-Wavenet-B" # صوت ذكوري وقور واحترافي
            )

            self._audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=0.88, # سرعة هادئة ومريحة للأذن
                pitch=-2.0          # نبرة عميقة تعطي وقار العلماء
            )
            logger.info("✅ تم تفعيل محرك Google Cloud TTS بنجاح")
        except Exception as e:
            logger.error(f"❌ فشل تهيئة GCP TTS: {str(e)}")
            raise

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=15))
    def synthesize(self, text: str, output_path: str):
        """توليد ملف صوتي MP3 من نص محدد"""
        if not text or len(text.strip()) < 2:
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

            # ننتظر أجزاء من الثانية لعدم إرهاق الـ API (Best practice)
            import time
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في توليد الصوت للنص: {text[:30]}... | الخطأ: {str(e)}")
            raise

    def generate_episode_audio(self, script, ep_dir) -> dict:
        """المحرك الرئيسي لتوليد كافة ملفات الصوت للحلقة"""
        logger.info(f"🎙️ بدء إنتاج أصوات الحلقة {script.episode_number}...")
        
        audio_map = {}  # 👈 إضافة القاموس لحفظ مسارات الملفات

        # 1. صوت مقدمة الجد
        intro_path = os.path.join(ep_dir, "intro_narrator.mp3")
        self.synthesize(script.intro_scene.narrator_text, intro_path)
        audio_map["intro"] = intro_path  # 👈 حفظ المسار في القاموس
        logger.info("✅ مقدمة الراوي جاهزة")

        # 2. أصوات مشاهد الآيات (التمهيد + الشرح)
        for i, scene in enumerate(script.ayah_scenes):
            # صوت التمهيد للآية (قبل التلاوة)
            p_intro = os.path.join(ep_dir, f"ayah_{scene.scene_id}_intro.mp3")
            self.synthesize(scene.intro_text, p_intro)
            audio_map[f"ayah_{scene.scene_id}_intro"] = p_intro  # 👈 حفظ المسار

            # صوت شرح المعنى (بعد التلاوة)
            p_explain = os.path.join(ep_dir, f"ayah_{scene.scene_id}_explain.mp3")
            self.synthesize(scene.explain_text, p_explain)
            audio_map[f"ayah_{scene.scene_id}_explain"] = p_explain  # 👈 حفظ المسار

            logger.info(f"✅ أصوات الآية {scene.ayah.number} جاهزة")

        # 3. صوت الخاتمة والوداع
        outro_path = os.path.join(ep_dir, "outro_narrator.mp3")
        self.synthesize(script.outro_scene.narrator_text, outro_path)
        audio_map["outro"] = outro_path  # 👈 حفظ المسار
        logger.info("✅ خاتمة الراوي جاهزة")

        logger.info("🎉 اكتمل إنتاج كافة الملفات الصوتية بنجاح!")
        return audio_map  # 👈 إرجاع القاموس بدلاً من True ليتوافق مع الـ Orchestrator
