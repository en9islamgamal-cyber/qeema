"""
main.py — VALUE / QEEMA v4.0 (Enterprise Edition - Enhanced)
نقطة الدخول الرئيسية (The Command Center)
• نظام Logging متقدم مع JSON Structured Logging (اختياري) و RotatingFileHandler محسن.
• فحص تشخيصي شامل للبيئة مع Path validation قبل الإطلاق.
• إغلاق آمن متقدم (Graceful Shutdown) يدعم SIGTERM/SIGINT مع priority handlers.
• دعم Environment-based logging levels و structured context.
• Validation متقدم للـ arguments باستخدام custom types/actions.
"""

import argparse
import json
import logging
import logging.config
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from dataclasses import dataclass
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

# ----------------------------------------------------------------------
# تكوين نظام التسجيل مع دعم JSON اختياري (Fault-tolerant)
# ----------------------------------------------------------------------
logs_dir = Path("logs")
logs_dir.mkdir(exist_ok=True)

if not os.access(logs_dir, os.W_OK):
    print("❌ خطأ: لا يمكن الكتابة في مجلد logs", file=sys.stderr)
    sys.exit(1)

# محاولة استيراد JsonFormatter إذا كانت المكتبة متاحة
JSON_LOGGER_AVAILABLE = False
json_formatter_class = None
try:
    from pythonjsonlogger import jsonlogger
    JSON_LOGGER_AVAILABLE = True
    json_formatter_class = jsonlogger.JsonFormatter
    print("✅ JSON structured logging متاحة (python-json-logger مثبت)", file=sys.stderr)
except ImportError:
    print("⚠️ تحذير: python-json-logger غير مثبت، سيتم استخدام logging نصي عادي.", file=sys.stderr)
    print("   للتثبيت: pip install python-json-logger", file=sys.stderr)

# بناء التكوين الأساسي (دون JSON في البداية)
log_config = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        },
        'file_text': {
            'format': '%(asctime)s [%(levelname)s] %(name)s - %(message)s (file:%(pathname)s line:%(lineno)d)',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'console',
            'stream': 'ext://sys.stdout'
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'DEBUG',
            'formatter': 'file_text',      # سنبدله لاحقاً إذا أمكن
            'filename': str(logs_dir / 'pipeline.log'),
            'maxBytes': 10 * 1024 * 1024,
            'backupCount': 5,
            'encoding': 'utf-8'
        }
    },
    'root': {
        'level': os.getenv('LOG_LEVEL', 'INFO'),
        'handlers': ['console', 'file']
    },
    'loggers': {
        'main': {
            'level': 'INFO',
            'handlers': ['console', 'file'],
            'propagate': False
        }
    }
}

# إذا كانت المكتبة متاحة، نضيف formatter JSON ونعدل ملف handler لاستخدامه
if JSON_LOGGER_AVAILABLE:
    log_config['formatters']['json'] = {
        '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
        'format': '%(asctime)s %(levelname)s %(name)s %(message)s %(pathname)s %(lineno)d %(process)d %(thread)d',
        'datefmt': '%Y-%m-%dT%H:%M:%S%z',
        'rename_fields': {
            'levelname': 'level',
            'asctime': 'timestamp',
            'name': 'logger',
            'pathname': 'file',
            'lineno': 'line',
        }
    }
    log_config['handlers']['file']['formatter'] = 'json'

# تطبيق التكوين (مع catch للأخطاء)
try:
    logging.config.dictConfig(log_config)
except Exception as e:
    print(f"❌ فشل تكوين logging: {e}", file=sys.stderr)
    # إذا فشل بسبب JSON، نحاول مرة أخرى بدون JSON
    if JSON_LOGGER_AVAILABLE:
        print("⚠️ إعادة المحاولة بدون JSON logging...", file=sys.stderr)
        log_config['handlers']['file']['formatter'] = 'file_text'
        # حذف formatter json إذا كان موجوداً
        log_config['formatters'].pop('json', None)
        try:
            logging.config.dictConfig(log_config)
        except Exception as e2:
            print(f"❌ فشل حتى بدون JSON: {e2}", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(1)

logger = logging.getLogger("main")

# ----------------------------------------------------------------------
# بقية الكود (ShutdownHandler, GracefulShutdownManager, دوال مساعدة, إلخ)
# ----------------------------------------------------------------------

@dataclass
class ShutdownHandler:
    name: str
    handler: Callable[[], None]
    timeout: float = 10.0
    priority: int = 0


class GracefulShutdownManager:
    def __init__(self, grace_period: float = 30.0):
        self.grace_period = grace_period
        self._shutdown_event = threading.Event()
        self._handlers: List[ShutdownHandler] = []
        self._in_flight = 0
        self._lock = threading.Lock()
        self._setup_signals()

    def _setup_signals(self):
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGQUIT, self._signal_handler)

    def _signal_handler(self, signum: int, frame):
        signal_name = signal.Signals(signum).name
        logger.info(f"Received {signal_name}, initiating graceful shutdown", extra={'signal': signal_name})
        self._shutdown_event.set()

    def register_handler(self, name: str, handler: Callable[[], None], timeout: float = 10.0, priority: int = 0):
        self._handlers.append(ShutdownHandler(name, handler, timeout, priority))

    @contextmanager
    def track_operation(self):
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def wait_for_shutdown(self):
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(0.1)

    def shutdown(self):
        logger.info("Starting graceful shutdown sequence")
        start = time.time()
        while self._in_flight > 0 and (time.time() - start) < self.grace_period * 0.5:
            time.sleep(0.1)
        handlers = sorted(self._handlers, key=lambda h: h.priority, reverse=True)
        for handler in handlers:
            try:
                logger.info(f"Executing shutdown handler: {handler.name}", extra={'handler': handler.name})
                handler.handler()
            except Exception as e:
                logger.error(f"Handler {handler.name} failed", exc_info=True)
        logger.info("Graceful shutdown complete")


shutdown_manager = GracefulShutdownManager()


def positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("Episode number must be positive")
    return ivalue


def print_banner():
    version = "v4.0 Enterprise Enhanced"
    banner = f"""
╔════════════════════════════════════════════════════╗
║        🚀 VALUE / QEEMA PIPELINE {version:^20}        ║
║      Enterprise Automated Production Engine        ║
║        Environment: {os.getenv('ENVIRONMENT', 'production')}          ║
╚════════════════════════════════════════════════════╝
    """
    logger.info("Pipeline starting", extra={'version': version})


def validate_env() -> List[str]:
    missing = []
    try:
        from config import APIKeys
        missing = APIKeys.validate()
    except ImportError:
        logger.error("Config module not found", exc_info=True)
        missing = ["config.APIKeys"]
    if not os.access(Path("logs"), os.W_OK):
        missing.append("logs directory writable")
    return missing


def print_status(orch):
    try:
        r = orch.db.table("episodes").select("*").order("episode_number").execute()
        logger.info("Episode status dashboard", extra={'total_episodes': len(r.data)})
        print("\n📊 [لوحة معلومات الحلقات - Episode Status Dashboard]")
        print(f"{'رقم':>4} | {'السورة':<12} | {'الحالة':<12} | {'رابط يوتيوب'}")
        print("═" * 65)
        for ep in r.data:
            from config import CURRICULUM
            sn = CURRICULUM.get(ep["episode_number"], {}).get("name", "—")
            status = ep.get('status', 'unknown').upper()
            url = ep.get('youtube_url', '—')
            print(f"{ep['episode_number']:>4} | {sn:<12} | {status:<12} | {url}")
        print("═" * 65)
    except Exception as e:
        logger.error("Failed to fetch status", exc_info=True)


def main():
    parser = argparse.ArgumentParser(
        description="VALUE / QEEMA Pipeline v4.0 Enterprise Enhanced",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --episode 1
  python main.py --dry-run --episode 1
  python main.py --status
        """
    )
    parser.add_argument("--episode", type=positive_int, help="رقم الحلقة المحددة")
    parser.add_argument("--dry-run", action="store_true", help="وضع الاختبار (لا رفع ليوتيوب)")
    parser.add_argument("--seed", action="store_true", help="بذر قاعدة البيانات بالمنهج")
    parser.add_argument("--status", action="store_true", help="عرض لوحة الحلقات")
    parser.add_argument("--list-voices", action="store_true", help="قائمة الأصوات المتاحة")
    args = parser.parse_args()

    print_banner()

    if args.list_voices:
        voices = [
            "ar-XA-Wavenet-B (الجد أبو زياد)",
            "ar-XA-Wavenet-A",
            "Charon",
            "Fenrir",
            "Puck"
        ]
        logger.info("Available AI voices", extra={'voices': voices})
        for v in voices:
            print(f"  • {v}")
        return

    missing = validate_env()
    if missing:
        logger.error("Environment validation failed", extra={'missing': missing})
        sys.exit(1)

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("Dry run mode activated")

    def orchestrator_cleanup():
        logger.debug("Orchestrator cleanup")

    shutdown_manager.register_handler("orchestrator", orchestrator_cleanup, priority=10)

    with shutdown_manager.track_operation():
        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator()

    if args.seed:
        if hasattr(orch, 'seed'):
            orch.seed()
        else:
            logger.error("Orchestrator has no seed method")
            sys.exit(1)
        return

    if args.status:
        print_status(orch)
        return

    try:
        with shutdown_manager.track_operation():
            if args.episode:
                logger.info("Producing specific episode", extra={'episode': args.episode})
                success = orch.run(args.episode)
            else:
                logger.info("Auto mode: next pending episode")
                if hasattr(orch, 'run_next'):
                    success = orch.run_next()
                else:
                    success = orch.run()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt received")
        sys.exit(0)
    except Exception as e:
        logger.critical("Unexpected failure", exc_info=True)
        sys.exit(1)
    finally:
        shutdown_manager.shutdown()


if __name__ == "__main__":
    main()