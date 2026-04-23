"""
main.py — VALUE / QEEMA v3.0 (Enterprise Edition)
نقطة الدخول الرئيسية (The Command Center)
• نظام Logging متقدم (RotatingFileHandler) لحماية مساحة السيرفر.
• فحص تشخيصي للبيئة قبل الإطلاق.
• إغلاق آمن (Graceful Shutdown) عند انقطاع التنفيذ.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

# 1. تجهيز بيئة السجلات (Logs)
Path("logs").mkdir(exist_ok=True)

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

# 👈 نظام الحماية: يمسح اللوجات القديمة تلقائياً إذا زاد حجمها عن 10 ميجا، ويحتفظ بآخر 5 ملفات فقط
file_handler = RotatingFileHandler(
    "logs/pipeline.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger("main")


def print_banner():
    """طباعة واجهة ترحيبية تشخيصية تؤكد أن النظام يعمل بالنسخة الحديثة"""
    banner = """
    ╔════════════════════════════════════════════════════╗
    ║        🚀 VALUE / QEEMA PIPELINE v3.0              ║
    ║      Enterprise Automated Production Engine        ║
    ╚════════════════════════════════════════════════════╝
    """
    print(banner)


def validate_env() -> list[str]:
    """فحص المتغيرات البيئية الضرورية قبل بدء أي عملية"""
    from config import APIKeys
    return APIKeys.validate()


def print_status(orch):
    """طباعة لوحة تحكم أنيقة لحالة الحلقات"""
    r = orch.db.table("episodes").select("*").order("episode_number").execute()
    print("\n📊 [لوحة معلومات الحلقات - Episode Status Dashboard]")
    print(f"{'رقم':>4} | {'السورة':<12} | {'الحالة':<12} | {'رابط يوتيوب'}")
    print("═"*65)
    for ep in r.data:
        from config import CURRICULUM
        sn = CURRICULUM.get(ep["episode_number"], {}).get("name", "—")
        status = ep.get('status', 'unknown').upper()
        url = ep.get('youtube_url', '—')
        print(f"{ep['episode_number']:>4} | {sn:<12} | {status:<12} | {url}")
    print("═"*65)


def main():
    parser = argparse.ArgumentParser(description="VALUE / QEEMA Pipeline v3.0")
    parser.add_argument("--episode", type=int,         help="تشغيل الإنتاج لرقم حلقة محددة")
    parser.add_argument("--dry-run", action="store_true", help="تجاوز رفع الفيديو إلى يوتيوب (للاختبار)")
    parser.add_argument("--seed",    action="store_true", help="بذر قاعدة البيانات بالمنهج (Curriculum)")
    parser.add_argument("--status",  action="store_true", help="عرض لوحة معلومات الحلقات")
    parser.add_argument("--list-voices", action="store_true", help="عرض قائمة أصوات Google/Gemini المتاحة")
    args = parser.parse_args()

    print_banner()

    if args.list_voices:
        print("\n🎙️ أصوات الذكاء الاصطناعي المتاحة:")
        for v in ["ar-XA-Wavenet-B (الجد أبو زياد)", "ar-XA-Wavenet-A", "Charon", "Fenrir", "Puck"]:
            print(f"  • {v}")
        return

    # فحص البيئة قبل الإطلاق لتجنب الانهيار في منتصف العمل
    missing = validate_env()
    if missing:
        logger.error(f"❌ توقف طارئ: هناك متغيرات بيئية (API Keys) مفقودة: {', '.join(missing)}")
        sys.exit(1)

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("🧪 [DRY RUN]: وضع الاختبار مفعّل (لن يتم نشر الفيديو على يوتيوب).")

    # تحميل قائد المنظومة بعد تأمين البيئة
    from orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator()

    if args.seed:
        orch.seed()
        return

    if args.status:
        print_status(orch)
        return

    # 👈 الإغلاق الآمن: التقاط إيقاف المستخدم لمنع تلف البيانات
    try:
        if args.episode:
            logger.info(f"🎯 تم توجيه الأمر لإنتاج الحلقة رقم: {args.episode}")
            success = orch.run(args.episode)
        else:
            logger.info("🤖 بدء وضع الإنتاج التلقائي (التقاط أول حلقة قيد الانتظار)...")
            success = orch.run_next()
            
        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        logger.warning("\n⚠️ تم إيقاف النظام يدوياً من قِبل المستخدم (Keyboard Interrupt).")
        logger.info("💾 لا تقلق، حالة الحلقة محفوظة ويمكن استئنافها لاحقاً.")
        sys.exit(130)
    except Exception as e:
        logger.critical(f"🔥 انهيار غير متوقع في النظام الأساسي: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
