"""
main.py — VALUE / QEEMA v2
نقطة الدخول الرئيسية
"""
import argparse, logging, os, sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

Path("logs").mkdir(exist_ok=True)


def validate_env() -> list[str]:
    from config import APIKeys
    return APIKeys.validate()


def main():
    parser = argparse.ArgumentParser(description="VALUE / QEEMA Pipeline v2")
    parser.add_argument("--episode", type=int,         help="رقم حلقة محددة")
    parser.add_argument("--dry-run", action="store_true", help="بدون رفع")
    parser.add_argument("--seed",    action="store_true", help="بذر قاعدة البيانات")
    parser.add_argument("--status",  action="store_true", help="عرض الحالة")
    parser.add_argument("--list-voices", action="store_true", help="عرض أصوات Gemini")
    args = parser.parse_args()

    if args.list_voices:
        print("\nأصوات Gemini TTS المتاحة:")
        for v in ["Aoede","Charon","Fenrir","Kore","Puck","Zephyr","Gacrux","Laomedeia","Achernar","Alnilam"]:
            print(f"  • {v}")
        return

    # تحقق البيئة
    missing = validate_env()
    if missing:
        logger.error(f"❌ متغيرات مفقودة: {', '.join(missing)}")
        sys.exit(1)

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("🧪 وضع الاختبار مفعّل")

    from orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator()

    if args.seed:
        orch.seed(); return

    if args.status:
        r = orch.db.table("episodes").select("*").order("episode_number").execute()
        print(f"\n{'رقم':>4}  {'السورة':<15}  {'الحالة':<15}  {'يوتيوب'}")
        print("─"*65)
        for ep in r.data:
            from config import CURRICULUM
            sn = CURRICULUM.get(ep["episode_number"],{}).get("name","—")
            print(f"{ep['episode_number']:>4}  {sn:<15}  {ep['status']:<15}  {ep.get('youtube_url','—')}")
        return

    success = orch.run(args.episode) if args.episode else orch.run_next()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
