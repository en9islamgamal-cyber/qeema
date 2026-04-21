"""
Orchestrator - QEEMA Pipeline
The Maestro that connects Script, Voice, Visual, and Video engines together.
Generates dynamic Karaoke-style subtitles for children.
"""

import os
import logging
from pathlib import Path

# استيراد محركاتنا الاحترافية
import script_engine
import voice_engine
import visual_engine
import video_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("qeema_orchestrator")

# =============================================================================
# 1. KARAOKE SUBTITLE GENERATOR (The Secret Sauce for Kids)
# =============================================================================

def generate_karaoke_ass(text: str, audio_duration: float, output_path: Path) -> None:
    """
    Creates an .ass subtitle file with Karaoke tags {\k} so words light up 
    as they are spoken. We use a smart approximation based on word count.
    """
    words = text.split()
    if not words:
        return

    # حساب وقت تقريبي لكل كلمة بالمللي ثانية (centiseconds لـ ASS)
    # نترك 10% من الوقت كصمت في البداية والنهاية
    effective_duration = audio_duration * 0.8
    time_per_word_cs = int((effective_duration / len(words)) * 100)
    
    # تنسيق الكاريوكي: {\k50}كلمة (يعني تظليل الكلمة لمدة 500 مللي ثانية)
    karaoke_text = " ".join([f"{{\\k{time_per_word_cs}}}{word}" for word in words])
    
    # قالب ملف ASS الاحترافي بخط عربي جميل ولون ذهبي للكلمة المنطوقة
    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Alignment, MarginV
Style: KaraokeStyle,Amiri,75,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,1,2,100

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:{audio_duration:05.2f},KaraokeStyle,,0,0,0,,{karaoke_text}
"""
    output_path.write_text(ass_content, encoding="utf-8")
    log.info(f"📝 تم إنشاء ملف الترجمة التفاعلية: {output_path.name}")

# =============================================================================
# 2. THE MAIN PIPELINE EXECUTION
# =============================================================================

def run_qeema_pipeline(surah_name: str, ayah_start: int, ayah_end: int):
    log.info("=" * 60)
    log.info(f"🚀 بدء إنتاج حلقة: سورة {surah_name} ({ayah_start}-{ayah_end})")
    log.info("=" * 60)

    # تجهيز مجلد العمل
    output_dir = Path("./qeema_output")
    output_dir.mkdir(exist_ok=True)
    assets_dir = Path("./assets")
    logo_path = assets_dir / "logo.png"

    # جلب المفاتيح من متغيرات البيئة
    AI_STUDIO_KEY = os.environ.get("GEMINI_API_KEY")
    LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")

    if not AI_STUDIO_KEY or not LEONARDO_API_KEY:
        log.error("❌ الرجاء التأكد من وضع مفاتيح API في متغيرات البيئة.")
        return

    try:
        # 1. المخرج يكتب السيناريو (Script Engine)
        script_data = script_engine.generate_cinematic_script(surah_name, ayah_start, ayah_end, AI_STUDIO_KEY)
        theme_colors = script_data.get("theme_colors", "warm pastel colors")
        
        scene_videos = []

        # 2. إنتاج كل مشهد خطوة بخطوة
        for i, scene in enumerate(script_data["scenes"]):
            log.info(f"🎬 جاري إنتاج المشهد {i+1}...")
            
            audio_path = output_dir / f"scene_{i}.mp3"
            image_path = output_dir / f"scene_{i}.png"
            ass_path = output_dir / f"scene_{i}.ass"
            video_path = output_dir / f"scene_{i}.mp4"

            # أ. الصوت البشري
            voice_engine.create_human_voiceover(scene["arabic_text"], audio_path, AI_STUDIO_KEY)
            
            # حساب مدة الصوت للترجمة والمونتاج
            audio_duration = video_engine.get_audio_duration(audio_path) # سنحتاج لإضافة هذه الدالة المساعدة في video_engine
            
            # ب. الصور السينمائية المتناسقة
            visual_engine.generate_professional_image(scene["visual_prompt"], image_path, LEONARDO_API_KEY, theme_colors)
            
            # ج. نصوص الكاريوكي الجاذبة للطفل
            generate_karaoke_ass(scene["arabic_text"], audio_duration, ass_path)
            
            # د. المونتاج ودمج اللوجو وحركة الكاميرا
            video_engine.assemble_cinematic_scene(image_path, audio_path, logo_path, video_path, scene.get("camera_movement", "zoom_in"))
            
            scene_videos.append(video_path)

        log.info("🎉 تم إنتاج جميع المشاهد بنجاح!")
        # الخطوة القادمة ستكون دمج هذه المشاهد (Concatenation) بمؤثرات انتقال ناعمة.

    except Exception as e:
        log.error(f"❌ حدث خطأ أثناء تنفيذ المنظومة: {e}")

if __name__ == "__main__":
    # لتشغيل البرنامج التجريبي
    # run_qeema_pipeline("الفيل", 1, 5)
    pass
