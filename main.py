"""
main.py — VALUE / QEEMA v4.0 (Enterprise Edition - Enhanced)
نقطة الدخول الرئيسية (The Command Center)
• نظام Logging متقدم مع JSON Structured Logging و RotatingFileHandler محسن.
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

# 1. تجهيز بيئة السجلات المتقدمة مع dictConfig للـ structured JSON logging [web:14][web:12]
logs_dir = Path("logs")
logs_dir.mkdir(exist_ok=True)

# Validate log directory permissions [web:4]
if not os.access(logs_dir, os.W_OK):
    print("❌ خطأ: لا يمكن الكتابة في مجلد logs", file=sys.stderr)
    sys.exit(1)

log_config: Dict = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        },
        'json': {
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
            'formatter': 'json',
            'filename': str(logs_dir / 'pipeline.log'),
            'maxBytes': 10 * 1024 * 1024,  # 10MB
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

logging.config.dictConfig(log_config)
logger = logging.getLogger("main")


@dataclass
class ShutdownHandler:
    """Shutdown handler configuration for graceful shutdown [web:3]"""
    name: str
    handler: Callable[[], None]
    timeout: float = 10.0
    priority: int = 0  # Higher = runs first


class GracefulShutdownManager:
    """Enterprise-grade graceful shutdown manager [web:3][web:17]"""
    
    def __init__(self, grace_period: float = 30.0):
        self.grace_period = grace_period
        self._shutdown_event = threading.Event()
        self._handlers: List[ShutdownHandler] = []
        self._in_flight = 0
        self._lock = threading.Lock()
        self._setup_signals()
    
    def _setup_signals(self):
        """Register signal handlers [web:8]"""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGQUIT, self._signal_handler)
    
    def _signal_handler(self, signum: int, frame):
        """Handle shutdown signals"""
        signal_name = signal.Signals(signum).name
        logger.info(f"Received {signal_name}, initiating graceful shutdown", extra={'signal': signal_name})
        self._shutdown_event.set()
    
    def register_handler(self, name: str, handler: Callable[[], None], timeout: float = 10.0, priority: int = 0):
        """Register cleanup handler"""
        self._handlers.append(ShutdownHandler(name, handler, timeout, priority))
    
    @contextmanager
    def track_operation(self):
        """Track in-flight operations"""
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
    
    def wait_for_shutdown(self):
        """Main loop wait point"""
        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(0.1)
    
    def shutdown(self):
        """Execute full shutdown sequence"""
        logger.info("Starting graceful shutdown sequence")
        
        # Wait for in-flight operations
        start = time.time()
        while self._in_flight > 0 and (time.time() - start) < self.grace_period * 0.5:
            time.sleep(0.1)
        
        # Execute handlers by priority
        handlers = sorted(self._handlers, key=lambda h: h.priority, reverse=True)
        for handler in handlers:
            try:
                logger.info(f"Executing shutdown handler: {handler.name}", extra={'handler': handler.name})
                # Simple timeout simulation (can be enhanced with concurrent.futures)
                handler.handler()
            except Exception as e:
                logger.error(f"Handler {handler.name} failed", exc_info=True)
        
        logger.info("Graceful shutdown complete")


# Global shutdown manager
shutdown_manager = GracefulShutdownManager()


def positive_int(value: str) -> int:
    """Custom validator for positive episode numbers [web:13]"""
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("Episode number must be positive")
    return ivalue


def print_banner():
    """Enhanced diagnostic banner"""
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
    """Enhanced environment validation [web:4]"""
    missing = []
    try:
        from config import APIKeys
        missing = APIKeys.validate()
    except ImportError:
        logger.error("Config module not found", exc_info=True)
        missing = ["config.APIKeys"]
    
    # Validate logs directory again
    if not os.access(Path("logs"), os.W_OK):
        missing.append("logs directory writable")
    
    return missing


def print_status(orch):
    """Enhanced status dashboard"""
    try:
        r = orch.db.table("episodes").select("*").order("episode_number").execute()
        logger.info("Episode status dashboard", extra={'total_episodes': len(r.data)})
        print("
📊 [لوحة معلومات الحلقات - Episode Status Dashboard]")
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
    # Enhanced argument parser with validation [web:2][web:7][web:13]
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
    
    # Comprehensive environment validation
    missing = validate_env()
    if missing:
        logger.error("Environment validation failed", extra={'missing': missing})
        sys.exit(1)
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        logger.info("Dry run mode activated")
    
    # Register shutdown handlers
    def orchestrator_cleanup():
        # Placeholder for orchestrator cleanup
        logger.debug("Orchestrator cleanup")
    
    shutdown_manager.register_handler("orchestrator", orchestrator_cleanup, priority=10)
    
    # Load orchestrator with operation tracking
    with shutdown_manager.track_operation():
        from orchestrator import PipelineOrchestrator
        orch = PipelineOrchestrator()
    
    if args.seed:
        orch.seed()
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
                success = orch.run_next()
        
        sys.exit(0 if success else 1)
    
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt received")
    except Exception as e:
        logger.critical("Unexpected failure", exc_info=True)
        sys.exit(1)
    finally:
        shutdown_manager.shutdown()


if __name__ == "__main__":
    main()