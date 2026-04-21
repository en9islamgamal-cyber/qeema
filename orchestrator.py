"""
Orchestrator - QEEMA Pipeline
The Maestro that connects all engines, tailored for pristine Islamic content.
"""

import os
import logging
import subprocess
from pathlib import Path

import script_engine
import voice_engine_v2 as voice_engine # محرك الصوت المطور
import visual_engine
import video_engine
import thumbnail_engine
import gamification_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("qeema_orchestrator")

def concat_videos(video_paths: list, output_path: Path):
    """دالة مساعدة لدمج المشاهد معاً بسلاسة"""
    list_file = Path("concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for vid in video_paths:
            f.write(f"file '{vid.resolve()}'\n")
    
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(output_path)
    ]
    subprocess.run(cmd, check=True)
    list_file.unlink()

def run_qeema_pipeline(surah_name: str, ayah_start: int, ayah_end: int) -> Path:
    log.info("=" * 60)
    log.info(f"🚀 بدء إنتاج حلقة: سورة {surah_name} ({ayah_start}-{ayah_end})")
    log.info("=" * 60)

    # 1. تجهيز المجلدات
    output_dir = Path("./qeema_output")
    output_dir.mkdir(exist_ok=True)
    assets_dir = Path("./assets")
    logo_path = assets_dir / "logo.png"

    # جلب المفاتيح
    AI_STUDIO_KEY = os.environ.get("GEMINI_API_KEY")
    LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")

    if not AI_STUDIO_KEY or not LEONARDO_API_KEY:
        raise ValueError("❌ مفاتيح API مفقودة!")

    # 2. المخرج يكتب السيناريو
    script_data = script_engine.generate_cinematic_script(surah_name, ayah_start, ayah_end, AI_STUDIO_KEY)
    theme_colors = script_data.get("theme_colors", "warm pastel colors")
    
    scene_videos = []
    thumbnail_image_source = None

    # 3. إنتاج المشهد الافتتاحي (Intro)
    intro_path = output_dir / "intro.mp4"
    if logo_path.exists():
        video_engine.create_branding_sequence(logo_path, intro_path, is_intro=True)
        scene_videos.append(intro_path)

    # 4. إنتاج المشاهد
    for i, scene in enumerate(script_data["scenes"]):
        log.info(f"🎬 إنتاج المشهد {i+1}...")
        
        audio_raw = output_dir / f"raw_audio_{i}.mp3"
        audio_final = output_dir / f"scene_{i}.mp3"
        image_path = output_dir / f"scene_{i}.png"
        video_path = output_dir / f"scene_{i}.mp4"

        # أ. الصوت المطور (بدون موسيقى، فقط معالجة للصوت البشري)
        ssml = voice_engine.text_to_advanced_ssml(scene["arabic_text"], scene.get("voice_emotion", "warm"))
        # ملاحظة: نستخدم دالة التوليد من API (يجب التأكد من دالة AI Studio في voice_engine)
        # لتسهيل الأمر، سنفترض أنها تولد الصوت الخام
        voice_engine.generate_voice_ai_studio(ssml, audio_raw, AI_STUDIO_KEY)
        voice_engine.apply_mastering_chain(audio_raw, audio_final)
        
        # ب. توليد الصورة
        visual_engine.generate_professional_image(scene["visual_prompt"], image_path, LEONARDO_API_KEY, theme_colors)
        if i == 0: thumbnail_image_source = image_path # حفظ أول صورة للصورة المصغرة
        
        # ج. المونتاج
        video_engine.assemble_cinematic_scene(
            image_path, audio_final, logo_path, video_path, 
            camera_movement=scene.get("camera_movement", "zoom_in")
        )
        scene_videos.append(video_path)

    # 5. إنتاج الخاتمة (Outro)
    outro_path = output_dir / "outro.mp4"
    if logo_path.exists():
        video_engine.create_branding_sequence(logo_path, outro_path, is_intro=False)
        scene_videos.append(outro_path)

    # 6. دمج الفيديو النهائي
    raw_final_video = output_dir / f"qeema_{surah_name}_raw.mp4"
    concat_videos(scene_videos, raw_final_video)

    # 7. محرك التحفيز (إضافة شريط التقدم التفاعلي)
    final_video_path = output_dir / f"qeema_{surah_name}_final.mp4"
    gamification_engine.add_interactive_progress_bar(raw_final_video, final_video_path)

    # 8. صانع الصور المصغرة
    thumbnail_path = output_dir / f"thumbnail_{surah_name}.jpg"
    if thumbnail_image_source:
        thumbnail_engine.create_pro_thumbnail(thumbnail_image_source, f"تفسير سورة {surah_name}", thumbnail_path)

    log.info(f"✨ اكتمل الإنتاج! الفيديو النهائي: {final_video_path}")
    return final_video_path
