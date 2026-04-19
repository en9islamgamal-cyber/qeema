"""
QEEMA / VALUE — Professional Qur'anic Tafseer Video Pipeline (v4.0)
====================================================================
Production-grade automated pipeline for generating broadcast-quality
Arabic Qur'anic educational videos for children.

Video Enhancements:
  - 1080p Full HD output
  - Ken Burns effect (smooth zoom/pan on images)
  - Hard-burned Arabic subtitles (synced with audio)
  - Fade transitions between scenes
  - Background music with audio ducking
  - Intro/Outro sequences
  - Channel branding/watermark
  - High-quality image generation (Leonardo Phoenix 1024x1024 upscaled)

Architecture:
  1) Supabase           -> State management
  2) Smart Chunking     -> Intelligent verse splitting
  3) Gemini 2.5 Flash   -> AI scene scripts
  4) Google Cloud TTS   -> Arabic WaveNet voiceover
  5) Leonardo.ai        -> High-quality illustrations
  6) FFmpeg (advanced)  -> Cinematic video assembly
  7) YouTube Data API   -> Automated upload

Author: Faramawy's / QEEMA VALUE
"""

import os
import sys
import json
import time
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import requests
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from google.cloud import texttospeech
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# =============================================================================
# 1. CONFIGURATION & LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qeema")

# --- Secrets ---
SUPABASE_URL          = os.environ.get("SUPABASE_URL")
SUPABASE_KEY          = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY")
LEONARDO_API_KEY      = os.environ.get("LEONARDO_API_KEY")
YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")

# --- AI Models ---
GEMINI_MODEL      = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TTS_VOICE_NAME    = os.environ.get("TTS_VOICE_NAME", "ar-XA-Wavenet-B")
TTS_SPEAKING_RATE = float(os.environ.get("TTS_SPEAKING_RATE", "0.88"))
TTS_PITCH         = float(os.environ.get("TTS_PITCH", "-2.0"))
LEONARDO_MODEL_ID = os.environ.get(
    "LEONARDO_MODEL_ID",
    "6b645e3a-d64f-4341-a6d8-7a3690fbf042"  # Phoenix 1.0
)

# --- Smart Chunking ---
SHORT_SURAH_THRESHOLD  = 15
MEDIUM_SURAH_THRESHOLD = 50
CHUNK_MEDIUM           = 8
CHUNK_LONG             = 7
TAIL_MERGE_THRESHOLD   = 3

# --- Video Production Settings ---
VIDEO_W, VIDEO_H    = 1920, 1080        # Full HD
IMAGE_W, IMAGE_H    = 1472, 832         # Leonardo (will be scaled up by ffmpeg)
FPS                 = 30
VIDEO_BITRATE       = "4M"
AUDIO_BITRATE       = "192k"
INTRO_DURATION      = 3.0
OUTRO_DURATION      = 3.0
TRANSITION_DURATION = 0.6
BG_MUSIC_VOLUME     = 0.12              # 12% of voiceover volume (ducking)
SUBTITLE_FONT_SIZE  = 42
SUBTITLE_MAX_CHARS  = 45                # per line

# --- Pipeline ---
OUTPUT_DIR          = Path("./qeema_output")
ASSETS_DIR          = Path("./assets")   # For bg music, logo, fonts
LEONARDO_POLL_TRIES = 18
LEONARDO_POLL_DELAY = 10
GEMINI_RETRIES      = 3
TTS_RETRIES         = 3
YOUTUBE_RETRIES     = 3

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# --- Validate secrets ---
_REQUIRED = {
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
    "GEMINI_API_KEY": GEMINI_API_KEY, "LEONARDO_API_KEY": LEONARDO_API_KEY,
    "GOOGLE_APPLICATION_CREDENTIALS": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
}
_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    log.error("❌ متغيرات بيئة ناقصة: %s", ", ".join(_missing))
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
tts_client = texttospeech.TextToSpeechClient()


# =============================================================================
# 2. QURAN SURAH DATABASE
# =============================================================================

SURAHS = [
    (1,"الفاتحة",7),(2,"البقرة",286),(3,"آل عمران",200),(4,"النساء",176),
    (5,"المائدة",120),(6,"الأنعام",165),(7,"الأعراف",206),(8,"الأنفال",75),
    (9,"التوبة",129),(10,"يونس",109),(11,"هود",123),(12,"يوسف",111),
    (13,"الرعد",43),(14,"إبراهيم",52),(15,"الحجر",99),(16,"النحل",128),
    (17,"الإسراء",111),(18,"الكهف",110),(19,"مريم",98),(20,"طه",135),
    (21,"الأنبياء",112),(22,"الحج",78),(23,"المؤمنون",118),(24,"النور",64),
    (25,"الفرقان",77),(26,"الشعراء",227),(27,"النمل",93),(28,"القصص",88),
    (29,"العنكبوت",69),(30,"الروم",60),(31,"لقمان",34),(32,"السجدة",30),
    (33,"الأحزاب",73),(34,"سبأ",54),(35,"فاطر",45),(36,"يس",83),
    (37,"الصافات",182),(38,"ص",88),(39,"الزمر",75),(40,"غافر",85),
    (41,"فصلت",54),(42,"الشورى",53),(43,"الزخرف",89),(44,"الدخان",59),
    (45,"الجاثية",37),(46,"الأحقاف",35),(47,"محمد",38),(48,"الفتح",29),
    (49,"الحجرات",18),(50,"ق",45),(51,"الذاريات",60),(52,"الطور",49),
    (53,"النجم",62),(54,"القمر",55),(55,"الرحمن",78),(56,"الواقعة",96),
    (57,"الحديد",29),(58,"المجادلة",22),(59,"الحشر",24),(60,"الممتحنة",13),
    (61,"الصف",14),(62,"الجمعة",11),(63,"المنافقون",11),(64,"التغابن",18),
    (65,"الطلاق",12),(66,"التحريم",12),(67,"الملك",30),(68,"القلم",52),
    (69,"الحاقة",52),(70,"المعارج",44),(71,"نوح",28),(72,"الجن",28),
    (73,"المزمل",20),(74,"المدثر",56),(75,"القيامة",40),(76,"الإنسان",31),
    (77,"المرسلات",50),(78,"النبأ",40),(79,"النازعات",46),(80,"عبس",42),
    (81,"التكوير",29),(82,"الانفطار",19),(83,"المطففين",36),(84,"الانشقاق",25),
    (85,"البروج",22),(86,"الطارق",17),(87,"الأعلى",19),(88,"الغاشية",26),
    (89,"الفجر",30),(90,"البلد",20),(91,"الشمس",15),(92,"الليل",21),
    (93,"الضحى",11),(94,"الشرح",8),(95,"التين",8),(96,"العلق",19),
    (97,"القدر",5),(98,"البينة",8),(99,"الزلزلة",8),(100,"العاديات",11),
    (101,"القارعة",11),(102,"التكاثر",8),(103,"العصر",3),(104,"الهمزة",9),
    (105,"الفيل",5),(106,"قريش",4),(107,"الماعون",7),(108,"الكوثر",3),
    (109,"الكافرون",6),(110,"النصر",3),(111,"المسد",5),(112,"الإخلاص",4),
    (113,"الفلق",5),(114,"الناس",6),
]
_SURAH_MAP = {num: (num, name, total) for num, name, total in SURAHS}


def get_surah_info(surah_num: int) -> Tuple[int, str, int]:
    if surah_num not in _SURAH_MAP:
        raise ValueError(f"رقم سورة غير صحيح: {surah_num}")
    return _SURAH_MAP[surah_num]


def calculate_chunk(surah_num: int, ayah_start: int) -> Tuple[int, bool]:
    _, name, total_verses = get_surah_info(surah_num)
    if ayah_start < 1 or ayah_start > total_verses:
        log.warning("⚠️ ayah_start=%d خارج النطاق، إعادة ضبط لـ 1", ayah_start)
        ayah_start = 1
    if total_verses <= SHORT_SURAH_THRESHOLD:
        log.info("📖 سورة %s قصيرة (%d آية) → فيديو واحد كامل", name, total_verses)
        return (total_verses, True)
    chunk_size = CHUNK_MEDIUM if total_verses <= MEDIUM_SURAH_THRESHOLD else CHUNK_LONG
    ayah_end = ayah_start + chunk_size - 1
    remaining_after = total_verses - ayah_end
    if 0 < remaining_after < TAIL_MERGE_THRESHOLD:
        ayah_end = total_verses
        log.info("🔗 ضم الآيات المتبقية (%d آيات)", remaining_after)
    if ayah_end >= total_verses:
        return (total_verses, True)
    return (ayah_end, False)


# =============================================================================
# 3. ASSET MANAGEMENT (Fonts, BG Music, Logo)
# =============================================================================

ARABIC_FONT_URL = "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf"
BG_MUSIC_URL = "https://cdn.pixabay.com/download/audio/2022/03/15/audio_c8e9a7e5d4.mp3"

FONT_PATH = ASSETS_DIR / "Amiri-Bold.ttf"
BG_MUSIC_PATH = ASSETS_DIR / "bg_music.mp3"


def ensure_asset(url: str, path: Path, name: str) -> bool:
    """Download an asset if missing. Returns True on success."""
    if path.exists() and path.stat().st_size > 1000:
        return True
    try:
        log.info("⬇️ تحميل %s...", name)
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log.info("✅ تم تحميل %s (%.1f KB)", name, path.stat().st_size / 1024)
        return True
    except Exception as e:
        log.warning("⚠️ فشل تحميل %s: %s", name, e)
        return False


def setup_assets() -> dict:
    """Download fonts & bg music. Returns dict of availability flags."""
    return {
        "font": ensure_asset(ARABIC_FONT_URL, FONT_PATH, "الخط العربي"),
        "bg_music": ensure_asset(BG_MUSIC_URL, BG_MUSIC_PATH, "الموسيقى الخلفية"),
    }


# =============================================================================
# 4. SCRIPT GENERATION (Gemini)
# =============================================================================

SYSTEM_PROMPT = """أنت شيخ أزهري جليل في قسم التفسير. تجتمع بأحفادك (أطفال 5-6 سنوات) لتفسر لهم آيات القرآن الكريم.
يجب أن يكون أسلوبك حنوناً، قصصياً، وبسيطاً جداً، كأنك جد يحكي قصة لأحفاده.
اذكر نص الآية أولاً، ثم قدم الشرح على لسان الشيخ الجد.

تحذير هام: المخرجات يجب أن تكون بتنسيق JSON صالح فقط (Valid JSON) بدون أي مقدمات أو نصوص قبله أو بعده.
الهيكل المطلوب حصراً:
{
  "scenes": [
    {
      "verse_text": "النص القرآني للآية",
      "narration": "شرح الشيخ الجد بأسلوب طفولي (2-3 جمل قصيرة مفصولة بنقطة)",
      "image_prompt": "وصف تفصيلي بالإنجليزية لصورة تناسب الشرح، بأسلوب كتب أطفال إسلامية، cinematic lighting, soft warm colors, highly detailed, بدون إظهار أي وجوه (no faces, no human features visible)"
    }
  ]
}"""


def _clean_json_text(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _validate_script(script: dict) -> None:
    if not isinstance(script, dict) or "scenes" not in script:
        raise ValueError("JSON بدون 'scenes'")
    scenes = script["scenes"]
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("'scenes' فارغة")
    for i, sc in enumerate(scenes):
        for key in ("verse_text", "narration", "image_prompt"):
            if key not in sc or not str(sc[key]).strip():
                raise ValueError(f"المشهد {i} ناقص: {key}")


def generate_script(surah_name: str, start: int, end: int) -> dict:
    user_prompt = f"المطلوب: تفسير سورة {surah_name} | الآيات: {start} إلى {end}."
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
    api_url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    last_err: Optional[Exception] = None
    for attempt in range(1, GEMINI_RETRIES + 1):
        log.info("🤖 Gemini (%s) — محاولة %d/%d", GEMINI_MODEL, attempt, GEMINI_RETRIES)
        try:
            r = requests.post(api_url, json=payload, timeout=120)
            if r.status_code != 200:
                log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:500])
                r.raise_for_status()
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise ValueError(f"لا توجد candidates")
            parts = candidates[0].get("content", {}).get("parts") or []
            if not parts:
                raise ValueError("رد Gemini بدون parts")
            raw = parts[0].get("text", "")
            script = json.loads(_clean_json_text(raw))
            _validate_script(script)
            log.info("✅ تم توليد السيناريو — %d مشاهد", len(script["scenes"]))
            return script
        except Exception as e:
            last_err = e
            log.warning("⚠️ فشل محاولة %d: %s", attempt, e)
            if attempt < GEMINI_RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"❌ فشل Gemini: {last_err}")


# =============================================================================
# 5. STATE MANAGEMENT (Supabase)
# =============================================================================

def load_state() -> dict:
    res = supabase.table("pipeline_state").select("*").eq("id", 1).execute()
    if res.data:
        s = res.data[0]
        if not s.get("surah_index"):
            s["surah_index"] = 1
        if not s.get("ayah_start"):
            s["ayah_start"] = 1
        return s
    default = {"id": 1, "surah_index": 1, "ayah_start": 1, "videos_published": 0}
    supabase.table("pipeline_state").insert(default).execute()
    return default


def save_state(surah_index: int, ayah_start: int, increment_videos: bool = True) -> None:
    current = supabase.table("pipeline_state")\
        .select("videos_published").eq("id", 1).execute()
    current_count = 0
    if current.data:
        current_count = current.data[0].get("videos_published") or 0
    new_count = current_count + 1 if increment_videos else current_count
    supabase.table("pipeline_state").upsert({
        "id": 1, "surah_index": surah_index, "ayah_start": ayah_start,
        "videos_published": new_count,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }).execute()
    log.info("💾 تم حفظ الحالة — الفيديوهات المنشورة: %d", new_count)


# =============================================================================
# 6. VOICEOVER (Google Cloud TTS)
# =============================================================================

def generate_voice(text: str, filename: Path) -> None:
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="ar-XA", name=TTS_VOICE_NAME,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=TTS_SPEAKING_RATE,
        pitch=TTS_PITCH,
        sample_rate_hertz=44100,
    )
    last_err: Optional[Exception] = None
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            response = tts_client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config,
            )
            with open(filename, "wb") as f:
                f.write(response.audio_content)
            size_kb = filename.stat().st_size / 1024
            if size_kb < 1:
                raise RuntimeError("ملف الصوت فارغ")
            log.info("   🔊 تم إنشاء الصوت (%.1f KB)", size_kb)
            return
        except Exception as e:
            last_err = e
            log.warning("⚠️ فشل TTS محاولة %d: %s", attempt, e)
            if attempt < TTS_RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"❌ فشل Google TTS: {last_err}")


def get_audio_duration(path: Path) -> float:
    """Get duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True
    )
    return float(result.stdout.strip())


# =============================================================================
# 7. IMAGE GENERATION (Leonardo.ai)
# =============================================================================

def generate_image(prompt: str, filename: Path) -> None:
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json", "accept": "application/json",
    }
    body = {
        "prompt": (
            "Cinematic children's Islamic storybook illustration, "
            "warm golden lighting, soft pastel colors, highly detailed, "
            "peaceful atmosphere, no faces, no human features: " + prompt
        ),
        "modelId": LEONARDO_MODEL_ID,
        "width": IMAGE_W, "height": IMAGE_H,
        "num_images": 1,
        "alchemy": True,              # Higher quality
        "photoReal": False,
        "presetStyle": "CINEMATIC",
    }
    r = requests.post(
        "https://cloud.leonardo.ai/api/rest/v1/generations",
        json=body, headers=headers, timeout=60,
    )
    if r.status_code not in (200, 201):
        # Retry without alchemy if it failed (some models don't support it)
        body.pop("alchemy", None)
        body.pop("presetStyle", None)
        r = requests.post(
            "https://cloud.leonardo.ai/api/rest/v1/generations",
            json=body, headers=headers, timeout=60,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Leonardo failed HTTP {r.status_code}: {r.text[:500]}")

    gen_id = (r.json().get("sdGenerationJob", {}).get("generationId")
              or r.json().get("generations_by_pk", {}).get("id"))
    if not gen_id:
        raise RuntimeError(f"لم يتم الحصول على generationId: {r.text[:500]}")

    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}"
    for i in range(LEONARDO_POLL_TRIES):
        time.sleep(LEONARDO_POLL_DELAY)
        pr = requests.get(poll_url, headers=headers, timeout=30)
        if pr.status_code != 200:
            continue
        job = pr.json().get("generations_by_pk", {}) or {}
        status = job.get("status")
        log.info("   Leonardo poll %d/%d — status=%s", i + 1, LEONARDO_POLL_TRIES, status)
        if status == "COMPLETE":
            images = job.get("generated_images") or []
            if not images:
                raise RuntimeError("Leonardo COMPLETE بدون صور")
            img = requests.get(images[0]["url"], timeout=60)
            img.raise_for_status()
            with open(filename, "wb") as f:
                f.write(img.content)
            return
        if status == "FAILED":
            raise RuntimeError(f"Leonardo FAILED: {job}")
    raise TimeoutError(f"Leonardo لم يكتمل")


# =============================================================================
# 8. SUBTITLE GENERATION (ASS format for Arabic RTL)
# =============================================================================

def _split_arabic_text(text: str, max_chars: int = SUBTITLE_MAX_CHARS) -> List[str]:
    """Split Arabic text into lines of max_chars."""
    words = text.split()
    lines = []
    current = ""
    for w in words:
        if len(current) + len(w) + 1 <= max_chars:
            current = f"{current} {w}".strip()
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _seconds_to_ass_time(t: float) -> str:
    """Convert seconds to ASS timestamp (H:MM:SS.cs)."""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_subtitle_ass(text: str, audio_duration: float, ass_path: Path) -> None:
    """Generate an ASS subtitle file for a single scene."""
    lines = _split_arabic_text(text)

    # ASS header
    header = f"""[Script Info]
Title: QEEMA Subtitle
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Amiri,{SUBTITLE_FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,3,3,2,2,80,80,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Distribute text evenly across audio duration
    # Allow 0.3s lead-in for first line
    lead_in = 0.2
    effective_duration = audio_duration - lead_in
    per_line = effective_duration / max(1, len(lines))

    events = []
    for i, line in enumerate(lines):
        t_start = lead_in + (i * per_line)
        t_end = lead_in + ((i + 1) * per_line)
        # Escape special chars & add Arabic RLM for RTL
        safe_line = line.replace("\n", " ").replace(",", "،")
        events.append(
            f"Dialogue: 0,{_seconds_to_ass_time(t_start)},{_seconds_to_ass_time(t_end)},"
            f"Default,,0,0,0,,{safe_line}"
        )

    ass_path.write_text(header + "\n".join(events), encoding="utf-8")


# =============================================================================
# 9. CINEMATIC SCENE ASSEMBLY (Ken Burns + Subtitles)
# =============================================================================

def _run_ffmpeg(cmd: list, description: str = "") -> None:
    log.info("   🎬 FFmpeg: %s", description or "processing")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("FFmpeg فشل:\nSTDERR:\n%s", proc.stderr[-2500:])
        raise RuntimeError(f"FFmpeg returned non-zero ({description})")


def assemble_scene_cinematic(
    audio_path: Path,
    img_path: Path,
    subtitle_text: str,
    out_path: Path,
    work_dir: Path,
    use_subtitles: bool = True,
    ken_burns_direction: str = "in",  # "in", "out", "left", "right"
) -> None:
    """
    Assemble a cinematic scene with Ken Burns effect + subtitles.

    Uses FFmpeg zoompan filter for smooth zoom/pan on still images.
    """
    duration = get_audio_duration(audio_path)
    total_frames = int(duration * FPS) + 1

    # Generate subtitles
    ass_path = work_dir / f"{out_path.stem}.ass"
    if use_subtitles and FONT_PATH.exists():
        generate_subtitle_ass(subtitle_text, duration, ass_path)

    # Ken Burns zoom expression
    # zoom from 1.0 to 1.12 over the scene duration
    if ken_burns_direction == "in":
        zoom_expr = f"zoom='min(zoom+0.0008,1.15)'"
        x_expr = "x='iw/2-(iw/zoom/2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"
    elif ken_burns_direction == "out":
        zoom_expr = f"zoom='if(eq(on,1),1.15,max(zoom-0.0008,1.0))'"
        x_expr = "x='iw/2-(iw/zoom/2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"
    elif ken_burns_direction == "left":
        zoom_expr = f"zoom='1.15'"
        x_expr = f"x='if(eq(on,1),0,x+2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"
    else:  # right
        zoom_expr = f"zoom='1.15'"
        x_expr = f"x='if(eq(on,1),iw-iw/zoom,x-2)'"
        y_expr = "y='ih/2-(ih/zoom/2)'"

    # Build filter chain
    vf_parts = [
        f"scale={VIDEO_W*2}:{VIDEO_H*2}:force_original_aspect_ratio=increase",
        f"crop={VIDEO_W*2}:{VIDEO_H*2}",
        f"zoompan={zoom_expr}:{x_expr}:{y_expr}:d={total_frames}:s={VIDEO_W}x{VIDEO_H}:fps={FPS}",
        f"format=yuv420p",
    ]

    # Add subtitles if available
    if use_subtitles and ass_path.exists() and FONT_PATH.exists():
        # fontsdir is the directory containing the Arabic font
        ass_filter_arg = str(ass_path).replace('\\', '/').replace(':', '\\:')
        fontsdir = str(ASSETS_DIR.resolve()).replace('\\', '/').replace(':', '\\:')
        vf_parts.append(f"ass='{ass_filter_arg}':fontsdir='{fontsdir}'")

    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(FPS), "-i", str(img_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-b:v", VIDEO_BITRATE, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run_ffmpeg(cmd, f"scene ({duration:.1f}s, Ken Burns {ken_burns_direction})")


def create_intro(surah_name: str, start: int, end: int, is_complete: bool,
                 out_path: Path, work_dir: Path) -> None:
    """Create a branded intro screen."""
    title_line = f"سورة {surah_name}"
    if is_complete:
        subtitle = "تفسير مبسّط للأطفال"
    else:
        subtitle = f"الآيات {start} إلى {end}"

    # Create solid color background with text using drawtext
    font_arg = ""
    if FONT_PATH.exists():
        font_path_escaped = str(FONT_PATH.resolve()).replace('\\', '/').replace(':', '\\:')
        font_arg = f":fontfile='{font_path_escaped}'"

    # Gradient background + title + subtitle
    vf = (
        f"[0:v]scale={VIDEO_W}:{VIDEO_H},format=yuv420p"
        f",drawtext=text='{title_line}'{font_arg}:fontsize=110:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-60:borderw=3:bordercolor=black@0.6"
        f",drawtext=text='{subtitle}'{font_arg}:fontsize=60:fontcolor=white@0.9:x=(w-text_w)/2:y=(h-text_h)/2+80:borderw=2:bordercolor=black@0.6"
        f",drawtext=text='قناة قيمة | VALUE'{font_arg}:fontsize=40:fontcolor=white@0.7:x=(w-text_w)/2:y=h-100"
        f",fade=t=in:st=0:d=0.5,fade=t=out:st={INTRO_DURATION-0.5}:d=0.5"
    )

    # Solid dark blue gradient background via color filter
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x1a2a4f:s={VIDEO_W}x{VIDEO_H}:d={INTRO_DURATION}:r={FPS}",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={INTRO_DURATION}",
        "-vf", vf.replace("[0:v]", ""),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-t", str(INTRO_DURATION),
        str(out_path),
    ]
    _run_ffmpeg(cmd, "intro")


def create_outro(out_path: Path, work_dir: Path) -> None:
    """Create a branded outro screen."""
    font_arg = ""
    if FONT_PATH.exists():
        font_path_escaped = str(FONT_PATH.resolve()).replace('\\', '/').replace(':', '\\:')
        font_arg = f":fontfile='{font_path_escaped}'"

    vf = (
        f"drawtext=text='جزاكم الله خيراً'{font_arg}:fontsize=100:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-40:borderw=3:bordercolor=black@0.6"
        f",drawtext=text='لا تنسوا الاشتراك والتعليق'{font_arg}:fontsize=55:fontcolor=white@0.9:x=(w-text_w)/2:y=(h-text_h)/2+70:borderw=2:bordercolor=black@0.6"
        f",drawtext=text='قناة قيمة | VALUE'{font_arg}:fontsize=45:fontcolor=0xffd700:x=(w-text_w)/2:y=h-100"
        f",fade=t=in:st=0:d=0.5,fade=t=out:st={OUTRO_DURATION-0.8}:d=0.8"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x0a1a3f:s={VIDEO_W}x{VIDEO_H}:d={OUTRO_DURATION}:r={FPS}",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={OUTRO_DURATION}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-t", str(OUTRO_DURATION),
        str(out_path),
    ]
    _run_ffmpeg(cmd, "outro")


def concat_with_crossfade(scene_files: List[Path], out_path: Path, work_dir: Path) -> None:
    """
    Concatenate scenes with crossfade transitions.
    Falls back to simple concat if crossfade fails.
    """
    if len(scene_files) < 2:
        # Single scene, just copy
        subprocess.run(["cp", str(scene_files[0]), str(out_path)], check=True)
        return

    # Build xfade filter chain
    # For N clips: [0:v][1:v] xfade -> [v1]; [v1][2:v] xfade -> [v2]; ...
    inputs = []
    for s in scene_files:
        inputs.extend(["-i", str(s)])

    # Get durations for offset calculation
    durations = [get_audio_duration(s) for s in scene_files]

    # Build video filter chain
    v_filters = []
    a_filters = []
    offset = 0.0
    current_v = "[0:v]"
    current_a = "[0:a]"

    for i in range(1, len(scene_files)):
        offset += durations[i - 1] - TRANSITION_DURATION
        next_v = f"[v{i}]"
        next_a = f"[a{i}]"
        v_filters.append(
            f"{current_v}[{i}:v]xfade=transition=fade:duration={TRANSITION_DURATION}:offset={offset}{next_v}"
        )
        a_filters.append(
            f"{current_a}[{i}:a]acrossfade=d={TRANSITION_DURATION}{next_a}"
        )
        current_v = next_v
        current_a = next_a

    filter_complex = ";".join(v_filters + a_filters)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", current_v, "-map", current_a,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(out_path),
    ]

    try:
        _run_ffmpeg(cmd, f"crossfade concat of {len(scene_files)} scenes")
    except Exception as e:
        log.warning("⚠️ فشل crossfade، استخدام concat عادي: %s", e)
        _simple_concat(scene_files, out_path, work_dir)


def _simple_concat(scene_files: List[Path], out_path: Path, work_dir: Path) -> None:
    """Fallback: simple concat without crossfade."""
    list_file = work_dir / "concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for s in scene_files:
            f.write(f"file '{Path(s).resolve()}'\n")
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        str(out_path),
    ], "simple concat")


def mix_background_music(video_path: Path, out_path: Path, work_dir: Path) -> None:
    """Mix background music under the video's voiceover with ducking."""
    if not BG_MUSIC_PATH.exists():
        log.info("⏭️ لا توجد موسيقى خلفية، تخطي الـ mixing")
        subprocess.run(["cp", str(video_path), str(out_path)], check=True)
        return

    video_duration = get_audio_duration(video_path)

    # Loop bg music to match video duration, reduce volume, and mix
    filter_complex = (
        f"[1:a]aloop=loop=-1:size=2e+09,atrim=0:{video_duration},"
        f"volume={BG_MUSIC_VOLUME},afade=t=in:st=0:d=1,afade=t=out:st={video_duration-1}:d=1[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(BG_MUSIC_PATH),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]

    try:
        _run_ffmpeg(cmd, "bg music mix")
    except Exception as e:
        log.warning("⚠️ فشل mix الموسيقى: %s", e)
        subprocess.run(["cp", str(video_path), str(out_path)], check=True)


# =============================================================================
# 10. YOUTUBE UPLOAD
# =============================================================================

def upload_to_youtube(video_path: Path, title: str, description: str) -> str:
    if not all([YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN]):
        raise RuntimeError("❌ بيانات YouTube OAuth ناقصة")
    creds = Credentials(
        token=None, refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    last_err: Optional[Exception] = None
    for attempt in range(1, YOUTUBE_RETRIES + 1):
        try:
            req = youtube.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": title[:100], "description": description[:5000],
                        "categoryId": "27", "defaultLanguage": "ar",
                        "defaultAudioLanguage": "ar",
                        "tags": ["قرآن", "تفسير", "أطفال", "تعليم", "إسلامي",
                                 "تفسير ميسر", "قصص القرآن",
                                 "Quran", "Tafseer", "Kids", "Islamic"],
                    },
                    "status": {"privacyStatus": "unlisted",
                               "selfDeclaredMadeForKids": True},
                },
                media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
            )
            return req.execute()["id"]
        except Exception as e:
            last_err = e
            log.warning("⚠️ فشل YouTube محاولة %d: %s", attempt, e)
            if attempt < YOUTUBE_RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"❌ فشل رفع YouTube: {last_err}")


# =============================================================================
# 11. MAIN PIPELINE
# =============================================================================

def run_pipeline() -> None:
    # Setup
    assets = setup_assets()
    log.info("🎨 الأصول: font=%s, bg_music=%s", assets["font"], assets["bg_music"])

    # Load state
    state = load_state()
    surah_num = int(state.get("surah_index", 1))
    start = int(state.get("ayah_start", 1))
    _, surah_name, total_verses = get_surah_info(surah_num)
    end, is_complete = calculate_chunk(surah_num, start)

    log.info("=" * 60)
    log.info("🎬 بدء المعالجة: سورة %s (رقم %d)", surah_name, surah_num)
    log.info("📖 الآيات: من %d إلى %d (إجمالي السورة: %d آية)",
             start, end, total_verses)
    log.info("=" * 60)

    try:
        # 1. Generate script
        script = generate_script(surah_name, start, end)

        # 2. Per-scene assets + cinematic assembly
        chunk_dir = OUTPUT_DIR / f"surah_{surah_num:03d}_{start}-{end}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        scene_files = []

        # Ken Burns direction rotation for variety
        kb_directions = ["in", "out", "left", "right"]

        for i, sc in enumerate(script["scenes"]):
            log.info("⏳ إنتاج المشهد %d/%d", i + 1, len(script["scenes"]))
            audio_p = chunk_dir / f"s{i}.mp3"
            img_p   = chunk_dir / f"s{i}.png"
            vid_p   = chunk_dir / f"s{i}.mp4"

            narration = f"{sc['verse_text']}. {sc['narration']}"
            subtitle_text = narration

            generate_voice(narration, audio_p)
            generate_image(sc["image_prompt"], img_p)
            assemble_scene_cinematic(
                audio_path=audio_p,
                img_path=img_p,
                subtitle_text=subtitle_text,
                out_path=vid_p,
                work_dir=chunk_dir,
                use_subtitles=assets["font"],
                ken_burns_direction=kb_directions[i % len(kb_directions)],
            )
            scene_files.append(vid_p)

        # 3. Create intro & outro
        log.info("🎬 إنشاء الـ Intro والـ Outro...")
        intro_p = chunk_dir / "intro.mp4"
        outro_p = chunk_dir / "outro.mp4"
        create_intro(surah_name, start, end, is_complete, intro_p, chunk_dir)
        create_outro(outro_p, chunk_dir)

        # 4. Concatenate: intro + scenes + outro
        all_scenes = [intro_p] + scene_files + [outro_p]
        combined_vid = chunk_dir / "combined.mp4"
        log.info("🎞️ دمج كل المشاهد مع transitions...")
        concat_with_crossfade(all_scenes, combined_vid, chunk_dir)

        # 5. Mix background music
        final_vid = OUTPUT_DIR / f"qeema_{surah_num:03d}_{surah_name}_{start}-{end}.mp4"
        log.info("🎵 إضافة الموسيقى الخلفية...")
        mix_background_music(combined_vid, final_vid, chunk_dir)

        log.info("✨ الفيديو النهائي جاهز: %s", final_vid)
        final_size_mb = final_vid.stat().st_size / (1024 * 1024)
        log.info("📦 حجم الفيديو: %.1f MB", final_size_mb)

        # 6. Metadata
        if is_complete and total_verses <= SHORT_SURAH_THRESHOLD:
            title = f"تفسير سورة {surah_name} للأطفال | قصة بأسلوب الجد"
            desc = (
                f"تفسير سورة {surah_name} كاملة ({total_verses} آيات) "
                f"بأسلوب قصصي مبسّط للأطفال من 5 إلى 10 سنوات.\n\n"
                f"🌟 قناة قيمة | VALUE 🌟\n"
                f"✨ تفسير قرآني احترافي بأسلوب الجد الحنون\n"
                f"🎨 رسوم فنية إسلامية\n"
                f"🎤 سرد صوتي بالفصحى\n\n"
                f"لا تنسوا الاشتراك والتعليق وتفعيل الجرس 🔔\n\n"
                f"#قرآن #تفسير_للأطفال #{surah_name} #قيمة #QEEMA #VALUE #Islamic_Kids"
            )
        else:
            title = f"سورة {surah_name} الآيات {start}-{end} | تفسير للأطفال"
            desc = (
                f"تفسير مبسط لسورة {surah_name} — الآيات من {start} إلى {end} "
                f"(من أصل {total_verses} آية).\n\n"
                f"🌟 قناة قيمة | VALUE 🌟\n"
                f"✨ تفسير قرآني احترافي بأسلوب الجد الحنون\n"
                f"🎨 رسوم فنية إسلامية\n"
                f"🎤 سرد صوتي بالفصحى\n\n"
                f"لا تنسوا الاشتراك والتعليق وتفعيل الجرس 🔔\n\n"
                f"#قرآن #تفسير_للأطفال #{surah_name} #قيمة #QEEMA #VALUE"
            )

        # 7. Upload to YouTube
        upload_success = False
        try:
            vid_id = upload_to_youtube(final_vid, title, desc)
            log.info("🌍 تم الرفع: https://youtu.be/%s", vid_id)
            upload_success = True
        except Exception as e:
            log.error("⚠️ فشل الرفع (الفيديو محفوظ محلياً): %s", e)

        # 8. Advance state
        if is_complete:
            next_surah = surah_num + 1 if surah_num < 114 else 1
            next_ayah = 1
            log.info("✅ تم الانتهاء من سورة %s كاملة", surah_name)
            if next_surah == 1:
                log.info("🔄 تم إكمال القرآن الكريم! بدء دورة جديدة")
            else:
                _, next_name, _ = get_surah_info(next_surah)
                log.info("➡️ التالي: سورة %s (رقم %d)", next_name, next_surah)
        else:
            next_surah = surah_num
            next_ayah = end + 1
            log.info("➡️ التالي: %s من الآية %d", surah_name, next_ayah)

        save_state(next_surah, next_ayah, increment_videos=upload_success)
        log.info("✅ المنظومة انتهت بنجاح")

    except Exception as e:
        log.exception("❌ فشل تماماً: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    run_pipeline()
