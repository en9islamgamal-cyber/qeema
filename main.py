import os
import json
import time
import requests
import io
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import google.generativeai as genai
from elevenlabs import generate, save
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import datetime

# 🔐 إعداد المتغيرات من بيئة GitHub Actions
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
LEONARDO_API_KEY = os.environ["LEONARDO_API_KEY"]
YOUTUBE_CLIENT_ID = os.environ["YOUTUBE_CLIENT_ID"]
YOUTUBE_CLIENT_SECRET = os.environ["YOUTUBE_CLIENT_SECRET"]
YOUTUBE_REFRESH_TOKEN = os.environ["YOUTUBE_REFRESH_TOKEN"]

OUTPUT_DIR = "./qeema_output"
LOGO_PATH = "./qeema_logo.png"
MAX_VERSES = 5
VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # صوت افتراضي مستقر (يمكن تغييره)

os.makedirs(OUTPUT_DIR, exist_ok=True)
genai.configure(api_key=GEMINI_API_KEY)

# 🟦 إدارة الحالة عبر Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def load_state():
    res = supabase.table("pipeline_state").select("*").execute()
    return res.data[0] if res.data else {"surah_index": 1, "ayah_start": 1, "videos_published": 0}

def save_state(state):
    supabase.table("pipeline_state").update(state).eq("id", 1).execute()

# 🎨 إنشاء اللوجو الثابت (مرة واحدة فقط)
def create_branding():
    if os.path.exists(LOGO_PATH): return
    size = (150, 150)
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, 140, 140], fill=(10, 60, 90, 240))
    font = ImageFont.load_default()
    draw.text((30, 65), "QEEMA", fill="white", font=font)
    img.save(LOGO_PATH)
create_branding()

# 📜 محرك Gemini المضاد للهَلوسة
SYSTEM_PROMPT = """أنت شيخ أزهري متخصص في تبسيط القرآن لأطفال 5-6 سنوات.
القواعد الصارمة:
1. استخدم فقط تفاسير معتمدة: ابن كثير، الجلالين، السعدي. لا تبتدع تفسيراً.
2. اذكر نص الآية بالعربية قبل الشرح مباشرة.
3. الأسلوب: قصصي، هادئ، محفز للحفظ، بلغة مصرية بسيطة جداً.
4. المخرجات بصيغة JSON صارمة فقط دون أي نص إضافي:
{
  "scenes": [
    {
      "verse_text": "نص الآية",
      "narration": "شرح مبسط + طريقة حفظ (قصة/تكرار/ربط)",
      "image_prompt": "وصف دقيق للصورة بأسلوب كتب أطفال تعليمية، ألوان هادئة، زخارف إسلامية هندسية، بدون وجوه بشرية، نمط موحد"
    }
  ]
}
5. إذا لم تتأكد، توقف واكتب 'مراجعة ضرورية'."""

def generate_script(surah_name, start, end):
    prompt = f"سورة: {surah_name} | الآيات: {start} إلى {end}. طبق القواعد بدقة."
    model = genai.GenerativeModel("gemini-2.0-flash-exp")
    res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json", "temperature": 0.2})
    return json.loads(res.text)

# 🔊 توليد الصوت
def generate_voice(text, filename):
    audio = generate(text=text, voice=VOICE_ID, model="eleven_multilingual_v2")
    save(audio, filename)

# 🖼️ توليد الصورة (مع انتظار ذكي)
def generate_image(prompt, filename):
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": f"Educational children's book style, soft pastel, Islamic geometric border, no human faces, consistent art style: {prompt}",
        "model_id": "leonardo-ai-phoenix-1.0",
        "width": 1280, "height": 720, "num_images": 1
    }
    res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers).json()
    gen_id = res.get("generations_by_pk", {}).get("id")
    if not gen_id: raise Exception("فشل توليد الصورة")

    for _ in range(6):  # انتظار حتى دقيقة
        time.sleep(10)
        status_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
        if status_res["generations_by_pk"]["status"] == "COMPLETE":
            img_url = status_res["generations_by_pk"]["generated_images"][0]["url"]
            r = requests.get(img_url)
            with open(filename, "wb") as f: f.write(r.content)
            return
    raise Exception("انتهت مهلة توليد الصورة")

# 🎬 تجميع الفيديو
def assemble_video(audio_path, img_path, out_path):
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", img_path, "-i", audio_path,
        "-vf", f"movie={LOGO_PATH}[logo];[in][logo]overlay=30:H-h-30:shortest=1",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", out_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)

def merge_scenes(scene_files, final_path):
    with open("concat.txt", "w") as f:
        for s in scene_files: f.write(f"file '{s}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", final_path]
    subprocess.run(cmd, check=True)

# 📤 رفع يوتيوب بدون متصفح (Headless)
def upload_to_youtube(video_path, title, description):
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    youtube = build("youtube", "v3", credentials=creds)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title, "description": description, "categoryId": "27", "tags": ["قرآن للأطفال", "قيمة"]},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True}
        },
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True)
    )
    return request.execute()["id"]

# 🚀 الدالة الرئيسية
def run_pipeline():
    state = load_state()
    surah_name = "الفاتحة"  # (يمكن ربطه بـ API القرآن لاحقاً)
    start, end = state["ayah_start"], min(state["ayah_start"] + MAX_VERSES - 1, 7)
    is_last = (end == 7)

    print(f"🎬 معالجة: {surah_name} ({start}-{end})")
    try:
        script = generate_script(surah_name, start, end)
    except Exception as e:
        print(f"❌ فشل التفسير: {e}")
        return

    scenes_dir = os.path.join(OUTPUT_DIR, f"chunk_{start}")
    os.makedirs(scenes_dir, exist_ok=True)
    scene_files = []

    for i, sc in enumerate(script["scenes"]):
        audio_p = os.path.join(scenes_dir, f"s{i}.mp3")
        img_p = os.path.join(scenes_dir, f"s{i}.png")
        vid_p = os.path.join(scenes_dir, f"s{i}.mp4")
        generate_voice(sc["narration"], audio_p)
        generate_image(sc["image_prompt"], img_p)
        assemble_video(audio_p, img_p, vid_p)
        scene_files.append(vid_p)
        print(f"✅ مشهد {i+1} مكتمل")

    final_vid = os.path.join(OUTPUT_DIR, f"qeema_{surah_name}_{start}.mp4")
    merge_scenes(scene_files, final_vid)

    title = f"سورة {surah_name} للأطفال | {start}-{end} | Value/Qeema"
    desc = f"شرح مبسط لتفسير الآيات {start}-{end} من سورة {surah_name} بأسلوب أزهري آمن.\n#قرآن_للأطفال #قيمة"
    
    vid_id = upload_to_youtube(final_vid, title, desc)
    print(f"🌍 تم الرفع: https://youtu.be/{vid_id}")

    # تحديث الحالة
    state["ayah_start"] = end + 1
    if is_last: state["ayah_start"] = 1
    state["videos_published"] += 1
    state["last_run"] = datetime.datetime.now().isoformat()
    save_state(state)
    print("✅ انتهت المهمة بنجاح")

if __name__ == "__main__":
    run_pipeline()