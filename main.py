"""
QEEMA / VALUE — Automated Qur'anic Tafseer Video Pipeline
=========================================================
Production-ready pipeline:
  1) Gemini 2.5  → generates scene script (verse + child-friendly narration + image prompt)
  2) ElevenLabs  → Arabic voiceover
  3) Leonardo.ai → illustration per scene
  4) FFmpeg      → assembles scenes into final video
  5) YouTube API → uploads as unlisted
  6) Supabase    → tracks progress (ayah_start)

Author: Faramawy's / QEEMA
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

import requests
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ElevenLabs — NEW SDK (v1.x+). The old `from elevenlabs import generate` API is deprecated.
from elevenlabs.client import ElevenLabs

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qeema")

# --- Secrets ---------------------------------------------------------------
SUPABASE_URL        = os.environ.get("SUPABASE_URL")
SUPABASE_KEY        = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY")
LEONARDO_API_KEY    = os.environ.get("LEONARDO_API_KEY")
YOUTUBE_CLIENT_ID   = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")

# --- Models / IDs (override via env if needed) -----------------------------
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
#   ↑ Use "gemini-2.5-pro" for highest quality on religious content (more expensive).
#   1.5 models are FULLY RETIRED by Google as of 2025 — do NOT use them.

ELEVEN_MODEL     = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")
VOICE_ID         = os.environ.get("ELEVEN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
#   ↑ Default is Rachel (English origin, works OK with multilingual_v2).
#   For authentic Arabic recitation tone, pick an Arabic voice from ElevenLabs voice library.

LEONARDO_MODEL_ID = os.environ.get(
    "LEONARDO_MODEL_ID",
    "6b645e3a-d64f-4341-a6d8-7a3690fbf042"  # Phoenix 1.0 UUID
)

# --- Pipeline settings -----------------------------------------------------
OUTPUT_DIR   = Path("./qeema_output")
MAX_VERSES   = 5
IMAGE_W, IMAGE_H = 1280, 720
LEONARDO_POLL_TRIES = 18   # 18 × 10s = 3 minutes max per image
LEONARDO_POLL_DELAY = 10
GEMINI_RETRIES = 3
ELEVEN_RETRIES = 3

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Validate critical secrets early --------------------------------------
_REQUIRED = {
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
    "GEMINI_API_KEY": GEMINI_API_KEY, "ELEVENLABS_API_KEY": ELEVENLABS_API_KEY,
    "LEONARDO_API_KEY": LEONARDO_API_KEY,
}
_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    log.error("❌ متغيرات بيئة ناقصة: %s", ", ".join(_missing))
    sys.exit(1)

# --- Clients ---------------------------------------------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)


# =============================================================================
# 2. SCRIPT GENERATION (Gemini)
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
      "narration": "شرح الشيخ الجد بأسلوب طفولي",
      "image_prompt": "وصف بالإنجليزية لصورة تناسب الشرح، بأسلوب كتب أطفال إسلامية، ألوان هادئة، بدون إظهار أي وجوه (no faces)"
    }
  ]
}"""


def _clean_json_text(text: str) -> str:
    """Remove markdown fences that Gemini sometimes wraps JSON in."""
    t = text.strip()
    if t.startswith("```"):
        # drop first fence line
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        # drop trailing fence
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _validate_script(script: dict) -> None:
    """Raise if structure is not usable for the pipeline."""
    if not isinstance(script, dict) or "scenes" not in script:
        raise ValueError("JSON بدون مفتاح 'scenes'")
    scenes = script["scenes"]
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("'scenes' فارغة أو ليست قائمة")
    for i, sc in enumerate(scenes):
        for key in ("verse_text", "narration", "image_prompt"):
            if key not in sc or not str(sc[key]).strip():
                raise ValueError(f"المشهد {i} ناقص الحقل: {key}")


def generate_script(surah_name: str, start: int, end: int) -> dict:
    """Call Gemini REST API and return validated scenes dict."""
    user_prompt = f"المطلوب: تفسير سورة {surah_name} | الآيات: {start} إلى {end}."
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    api_url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
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
                raise ValueError(f"لا توجد candidates في رد Gemini: {data}")
            parts = candidates[0].get("content", {}).get("parts") or []
            if not parts:
                raise ValueError(f"رد Gemini بدون parts: {data}")

            raw = parts[0].get("text", "")
            cleaned = _clean_json_text(raw)
            script = json.loads(cleaned)
            _validate_script(script)
            log.info("✅ تم توليد السيناريو — %d مشاهد", len(script["scenes"]))
            return script

        except Exception as e:
            last_err = e
            log.warning("⚠️ فشل محاولة %d: %s", attempt, e)
            if attempt < GEMINI_RETRIES:
                time.sleep(3 * attempt)  # backoff

    raise RuntimeError(f"❌ فشل Gemini بعد {GEMINI_RETRIES} محاولات: {last_err}")


# =============================================================================
# 3. STATE (Supabase)
# =============================================================================

def load_state() -> dict:
    res = supabase.table("pipeline_state").select("*").eq("id", 1).execute()
    if res.data:
        return res.data[0]
    # Initialize row if missing
    default = {"id": 1, "ayah_start": 1}
    supabase.table("pipeline_state").insert(default).execute()
    return default


def save_state(state: dict) -> None:
    supabase.table("pipeline_state").upsert({**state, "id": 1}).execute()


# =============================================================================
# 4. VOICEOVER (ElevenLabs — new SDK)
# =============================================================================

def generate_voice(text: str, filename: Path) -> None:
    last_err: Optional[Exception] = None
    for attempt in range(1, ELEVEN_RETRIES + 1):
        try:
            audio_iter = eleven_client.text_to_speech.convert(
                voice_id=VOICE_ID,
                model_id=ELEVEN_MODEL,
                text=text,
                output_format="mp3_44100_128",
            )
            with open(filename, "wb") as f:
                for chunk in audio_iter:
                    if chunk:
                        f.write(chunk)
            if filename.stat().st_size < 1000:
                raise RuntimeError("ملف الصوت الناتج فاضي/صغير جداً")
            return
        except Exception as e:
            last_err = e
            log.warning("⚠️ فشل ElevenLabs محاولة %d: %s", attempt, e)
            if attempt < ELEVEN_RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"❌ فشل ElevenLabs: {last_err}")


# =============================================================================
# 5. IMAGE GENERATION (Leonardo.ai)
# =============================================================================

def generate_image(prompt: str, filename: Path) -> None:
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }
    body = {
        "prompt": (
            "Educational children's book illustration, soft warm colors, "
            "Islamic art style, no faces, no people shown directly: " + prompt
        ),
        "modelId": LEONARDO_MODEL_ID,
        "width": IMAGE_W,
        "height": IMAGE_H,
        "num_images": 1,
    }

    # 1. Trigger generation
    r = requests.post(
        "https://cloud.leonardo.ai/api/rest/v1/generations",
        json=body, headers=headers, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Leonardo create failed HTTP {r.status_code}: {r.text[:500]}")

    gen_id = r.json().get("sdGenerationJob", {}).get("generationId") \
             or r.json().get("generations_by_pk", {}).get("id")
    if not gen_id:
        raise RuntimeError(f"لم يتم الحصول على generationId من Leonardo: {r.text[:500]}")

    # 2. Poll for completion
    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}"
    for i in range(LEONARDO_POLL_TRIES):
        time.sleep(LEONARDO_POLL_DELAY)
        pr = requests.get(poll_url, headers=headers, timeout=30)
        if pr.status_code != 200:
            log.warning("Leonardo poll HTTP %s", pr.status_code)
            continue
        job = pr.json().get("generations_by_pk", {}) or {}
        status = job.get("status")
        log.info("   Leonardo poll %d/%d — status=%s", i + 1, LEONARDO_POLL_TRIES, status)
        if status == "COMPLETE":
            images = job.get("generated_images") or []
            if not images:
                raise RuntimeError("Leonardo COMPLETE لكن بدون صور")
            img_url = images[0]["url"]
            img = requests.get(img_url, timeout=60)
            img.raise_for_status()
            with open(filename, "wb") as f:
                f.write(img.content)
            return
        if status == "FAILED":
            raise RuntimeError(f"Leonardo أعاد FAILED: {job}")

    raise TimeoutError(f"Leonardo لم يكتمل خلال {LEONARDO_POLL_TRIES * LEONARDO_POLL_DELAY}s")


# =============================================================================
# 6. VIDEO ASSEMBLY (FFmpeg)
# =============================================================================

def _run_ffmpeg(cmd: list) -> None:
    """Run ffmpeg, surface stderr if it fails."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("FFmpeg فشل:\nCMD: %s\nSTDERR:\n%s", " ".join(cmd), proc.stderr[-2000:])
        raise RuntimeError("FFmpeg returned non-zero")


def assemble_scene(audio_path: Path, img_path: Path, out_path: Path) -> None:
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(img_path),
        "-i", str(audio_path),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_path),
    ])


def concat_scenes(scene_files: list, out_path: Path, work_dir: Path) -> None:
    list_file = work_dir / "concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for s in scene_files:
            f.write(f"file '{Path(s).resolve()}'\n")
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ])


# =============================================================================
# 7. YOUTUBE UPLOAD
# =============================================================================

def upload_to_youtube(video_path: Path, title: str, description: str) -> str:
    if not all([YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN]):
        raise RuntimeError("❌ بيانات YouTube OAuth ناقصة")

    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    req = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title[:100],
                "description": description,
                "categoryId": "27",  # Education
                "defaultLanguage": "ar",
                "defaultAudioLanguage": "ar",
            },
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True},
        },
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
    )
    resp = req.execute()
    return resp["id"]


# =============================================================================
# 8. MAIN PIPELINE
# =============================================================================

def run_pipeline():
    state = load_state()
    start = int(state.get("ayah_start", 1))
    surah = "الفاتحة"
    end = min(start + MAX_VERSES - 1, 7)
    log.info("🎬 بدء المعالجة: سورة %s — الآيات %d إلى %d", surah, start, end)

    try:
        # 1) Script
        script = generate_script(surah, start, end)

        # 2) Per-scene assets
        chunk_dir = OUTPUT_DIR / f"chunk_{start}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        scene_files = []

        for i, sc in enumerate(script["scenes"]):
            log.info("⏳ إنتاج المشهد %d/%d", i + 1, len(script["scenes"]))
            audio_p = chunk_dir / f"s{i}.mp3"
            img_p   = chunk_dir / f"s{i}.png"
            vid_p   = chunk_dir / f"s{i}.mp4"

            narration = f"{sc['verse_text']}. {sc['narration']}"
            generate_voice(narration, audio_p)
            generate_image(sc["image_prompt"], img_p)
            assemble_scene(audio_p, img_p, vid_p)
            scene_files.append(vid_p)

        # 3) Concat into final video
        final_vid = OUTPUT_DIR / f"qeema_{surah}_{start}.mp4"
        concat_scenes(scene_files, final_vid, chunk_dir)
        log.info("🎞️ الفيديو النهائي جاهز: %s", final_vid)

        # 4) Upload
        title = f"سورة {surah} للأطفال — من الآية {start} إلى {end}"
        desc  = (
            f"تفسير مبسط لسورة {surah} (الآيات {start}-{end}) بأسلوب قصصي للأطفال.\n"
            f"إنتاج: QEEMA / VALUE — قناة Faramawy's التعليمية."
        )
        try:
            vid_id = upload_to_youtube(final_vid, title, desc)
            log.info("🌍 تم الرفع: https://youtu.be/%s", vid_id)
        except Exception as e:
            log.error("⚠️ فشل الرفع على يوتيوب (الفيديو محفوظ محلياً): %s", e)

        # 5) Advance state
        next_start = 1 if end >= 7 else end + 1
        save_state({"ayah_start": next_start})
        log.info("✅ تم بنجاح — الآية التالية ستبدأ من: %d", next_start)

    except Exception as e:
        log.exception("❌ فشل تماماً: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    run_pipeline()
