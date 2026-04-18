import os
import json
import time
import requests
import subprocess
import datetime
from PIL import Image, ImageDraw, ImageFont
import google.generativeai as genai
import anthropic 
from elevenlabs import generate, save, set_api_key
from supabase import create_client, Client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 🔐 السكرتارية التقنية (Secrets)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") 
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
LEONARDO_API_KEY = os.environ.get("LEONARDO_API_KEY")
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")

# ضبط المفاتيح
set_api_key(ELEVENLABS_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)

OUTPUT_DIR = "./qeema_output"
LOGO_PATH = "./qeema_logo.png"
MAX_VERSES = 5
VOICE_ID = "21m00Tcm4TlvDq8ikWAM" 

os.makedirs(OUTPUT_DIR, exist_ok=True)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 📜 الـ Prompt الأساسي (الدستور الأزهري)
SYSTEM_PROMPT = """أنت شيخ أزهري متخصص في تبسيط القرآن لأطفال 5-6 سنوات.
استخدم لغة مصرية بسيطة جداً وقصصية. اذكر نص الآية أولاً ثم الشرح.
المخرجات يجب أن تكون بصيغة JSON فقط:
{
  "scenes": [
    {
      "verse_text": "نص الآية",
      "narration": "الشرح المبسط",
      "image_prompt": "وصف دقيق للصورة بأسلوب كتب أطفال، بدون وجوه"
    }
  ]
}"""

def generate_script(surah_name, start, end):
    prompt = f"سورة: {surah_name} | الآيات: {start} إلى {end}. طبق القواعد بدقة."
    full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
    
    # 1️⃣ الخطة أ: Gemini (بأسماء موديلات مختلفة لتجنب الـ 404)
    for model_name in ["gemini-1.5-flash", "gemini-1.5-pro"]:
        print(f"🤖 محاولة عبر Gemini ({model_name})...")
        try:
            model = genai.GenerativeModel(model_name)
            res = model.generate_content(
                full_prompt, 
                generation_config={"response_mime_type": "application/json", "temperature": 0.2}
            )
            return json.loads(res.text)
        except Exception as e:
            print(f"⚠️ {model_name} فشل: {e}")

    # 2️⃣ الخطة ب: Claude (المنقذ في الأزمات)
    if ANTHROPIC_API_KEY:
        print("🧠 تحويل المهمة إلى Claude (البديل الإستراتيجي)...")
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            message = client.messages.create(
                model="claude-3-5-sonnet-20240620", 
                max_tokens=2000,
                temperature=0.2,
                system="أنت شيخ أزهري، أجب بصيغة JSON فقط.",
                messages=[{"role": "user", "content": full_prompt}]
            )
            return json.loads(message.content[0].text)
        except Exception as ce:
            print(f"❌ حتى Claude فشل: {ce}")
            
    raise Exception("فشلت كل المحاولات لتوليد التفسير.")

# --- باقي الدوال (توليد الصور والمونتاج والرفع) كما هي ---
def load_state():
    res = supabase.table("pipeline_state").select("*").execute()
    return res.data[0] if res.data else {"ayah_start": 1}

def save_state(state):
    supabase.table("pipeline_state").update(state).eq("id", 1).execute()

def generate_voice(text, filename):
    audio = generate(text=text, voice=VOICE_ID, model="eleven_multilingual_v2")
    save(audio, filename)

def generate_image(prompt, filename):
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": f"Educational children's book style, soft colors, Islamic art, no faces: {prompt}",
        "model_id": "leonardo-ai-phoenix-1.0", "width": 1280, "height": 720, "num_images": 1
    }
    res = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers).json()
    gen_id = res.get("generations_by_pk", {}).get("id")
    for _ in range(12):
        time.sleep(10)
        st = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
        if st["generations_by_pk"]["status"] == "COMPLETE":
            r = requests.get(st["generations_by_pk"]["generated_images"][0]["url"])
            with open(filename, "wb") as f: f.write(r.content)
            return

def assemble_video(audio_path, img_path, out_path):
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", out_path]
    subprocess.run(cmd, check=True, capture_output=True)

def upload_to_youtube(video_path, title, description):
    creds = Credentials(token=None, refresh_token=YOUTUBE_REFRESH_TOKEN, token_uri="https://oauth2.googleapis.com/token", client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET, scopes=["https://www.googleapis.com/auth/youtube.upload"])
    youtube = build("youtube", "v3", credentials=creds)
    request = youtube.videos().insert(part="snippet,status", body={"snippet": {"title": title, "description": description, "categoryId": "27"}, "status": {"privacyStatus": "unlisted"}}, media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True))
    return request.execute()["id"]

def run_pipeline():
    state = load_state()
    start = state["ayah_start"]
    print(f"🎬 معالجة: الفاتحة من الآية {start}")
    try:
        script = generate_script("الفاتحة", start, start + MAX_VERSES - 1)
        scenes_dir = os.path.join(OUTPUT_DIR, f"chunk_{start}")
        os.makedirs(scenes_dir, exist_ok=True)
        scene_files = []
        for i, sc in enumerate(script["scenes"]):
            print(f"⏳ إنتاج المشهد {i+1}...")
            audio_p, img_p, vid_p = os.path.join(scenes_dir, f"s{i}.mp3"), os.path.join(scenes_dir, f"s{i}.png"), os.path.join(scenes_dir, f"s{i}.mp4")
            generate_voice(sc["narration"], audio_p)
            generate_image(sc["image_prompt"], img_p)
            assemble_video(audio_p, img_p, vid_p)
            scene_files.append(vid_p)
        
        # دمج بسيط للنتائج (تجاوزاً للوقت)
        final_vid = os.path.join(OUTPUT_DIR, f"qeema_{start}.mp4")
        with open("list.txt", "w") as f:
            for s in scene_files: f.write(f"file '{os.path.abspath(s)}'\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", final_vid], check=True)
        
        vid_id = upload_to_youtube(final_vid, f"سورة الفاتحة للأطفال - آية {start}", "تفسير مبسط")
        print(f"🌍 تم الرفع: https://youtu.be/{vid_id}")
        
        state["ayah_start"] = 1 if start + MAX_VERSES > 7 else start + MAX_VERSES
        save_state(state)
        print("✅ تم بنجاح")
    except Exception as e:
        print(f"❌ فشل تماماً: {e}")

if __name__ == "__main__":
    run_pipeline()