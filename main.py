"""
Main Entry Point - QEEMA Pipeline
Manages Supabase state, triggers the Orchestrator, and handles YouTube uploads.
"""

import os
import logging
from supabase import create_client, Client
import orchestrator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qeema_main")

# إعداد Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None
    log.warning("⚠️ لم يتم العثور على مفاتيح Supabase. سيعمل النظام محلياً فقط.")

def load_state() -> dict:
    if not supabase: return {"surah_index": 1, "ayah_start": 1}
    res = supabase.table("pipeline_state").select("*").eq("id", 1).execute()
    if res.data:
        return res.data[0]
    default = {"id": 1, "surah_index": 1, "ayah_start": 1, "videos_published": 0}
    supabase.table("pipeline_state").insert(default).execute()
    return default

def save_state(surah_index: int, ayah_start: int):
    if not supabase: return
    current = supabase.table("pipeline_state").select("videos_published").eq("id", 1).execute()
    count = current.data[0].get("videos_published", 0) + 1 if current.data else 1
    supabase.table("pipeline_state").upsert({
        "id": 1, "surah_index": surah_index, "ayah_start": ayah_start, "videos_published": count
    }).execute()
    log.info(f"💾 تم حفظ الحالة الجديدة. الفيديوهات المنشورة: {count}")

def main():
    state = load_state()
    surah_num = int(state.get("surah_index", 105)) # كمثال: سورة الفيل (105)
    ayah_start = int(state.get("ayah_start", 1))
    
    # قائمة مؤقتة للسور (يمكنك توسيعها كما في ملفك الأصلي)
    SURAHS = {105: ("الفيل", 5), 106: ("قريش", 4), 107: ("الماعون", 7)}
    
    surah_name, total_ayahs = SURAHS.get(surah_num, ("الفاتحة", 7))
    ayah_end = min(ayah_start + 4, total_ayahs) # نأخذ 5 آيات لكل فيديو

    try:
        # تشغيل الإنتاج
        final_video = orchestrator.run_qeema_pipeline(surah_name, ayah_start, ayah_end)
        
        # تحديث الحالة للمرة القادمة
        if ayah_end >= total_ayahs:
            next_surah = surah_num + 1 if surah_num < 114 else 1
            next_ayah = 1
        else:
            next_surah = surah_num
            next_ayah = ayah_end + 1
            
        save_state(next_surah, next_ayah)
        log.info("✅ انتهت دورة العمل بنجاح تام.")
        
        # (ملاحظة: يمكنك إضافة كود الرفع لليوتيوب هنا باستخدام YouTube API)

    except Exception as e:
        log.error(f"❌ توقفت المنظومة بسبب خطأ: {e}")

if __name__ == "__main__":
    main()
