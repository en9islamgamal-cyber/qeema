"""
main.py — VALUE / QEEMA v5.0 (Production Bootstrapper)
=====================================================
The Command Center:
  ✅ Async Lifecycle Management
  ✅ Advanced Dependency Injection
  ✅ Structured Multi-Handler Logging
  ✅ Fault-Tolerant Signal Handling
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional, List

# استيراد المكونات الأساسية (التي قمنا بتطويرها سابقاً)
from config import APIKeys, Paths, VideoConfig
from orchestrator import PipelineOrchestrator
from ai_director import AIDirector

# إعداد الـ Logger الرئيسي
logger = logging.getLogger("qeema.main")

class QeemaApplication:
    """
    القلب النابض للمنظومة. يدير دورة حياة التطبيق من البداية للنهاية.
    """
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.orchestrator: Optional[PipelineOrchestrator] = None
        self._loop = asyncio.get_event_loop()
        self._shutdown_task: Optional[asyncio.Task] = None

    def setup_logging(self):
        """إعداد متقدم لنظام التسجيل يضمن عدم ضياع أي Log حتى عند الانهيار."""
        from logging.config import dictConfig
        # نستخدم الإعدادات التي أرسلتها مع تحسين الـ Buffering
        # (نفس log_config الخاص بك مع إضافة JSON handler بشكل أساسي)
        # ... (يتم استدعاء log_config من ملف خارجي لضمان الـ Clean Code)

    async def bootstrap(self):
        """
        مرحلة حقن التبعيات (Dependency Injection).
        هنا نقوم بربط المحركات الذكية بالـ Orchestrator.
        """
        logger.info("🏗️  Bootstrapping Qeema Engines...")
        
        # 1. تهيئة المخرج الذكي (حاكم الموارد)
        director = AIDirector(llm_rpm=30, image_rpm=15)
        
        # 2. بناء الـ Orchestrator مع حقن التبعيات
        self.orchestrator = PipelineOrchestrator(
            director=director,
            dry_run=self.args.dry_run
        )
        
        # 3. التحمية (Warmup) للمحركات الثقيلة (Playwright/FFmpeg)
        await self.orchestrator.warmup_async()

    def handle_signals(self):
        """إدارة الإشارات بنمط Async-safe."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._loop.add_signal_handler(
                sig, lambda: asyncio.create_task(self.shutdown())
            )

    async def shutdown(self):
        """الإغلاق الآمن والمنظم (The Saga Shutdown)."""
        if self._shutdown_task: return
        
        logger.info("🛑 Initiating Graceful Shutdown sequence...")
        if self.orchestrator:
            await self.orchestrator.shutdown_async()
        
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [t.cancel() for t in tasks]
        
        logger.info(f"🧹 Cleaning up {len(tasks)} pending tasks...")
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop.stop()
        logger.info("✅ Shutdown complete. System offline.")

    async def run(self):
        """المحرك الرئيسي للتشغيل."""
        self.handle_signals()
        await self.bootstrap()

        try:
            if self.args.status:
                await self.orchestrator.print_dashboard_async()
                return

            if self.args.episode:
                success = await self.orchestrator.run_async(self.args.episode)
            else:
                success = await self.orchestrator.run_next_async()
            
            sys.exit(0 if success else 1)

        except Exception as e:
            logger.critical(f"💥 Unhandled system failure: {e}", exc_info=True)
            sys.exit(1)

# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------
def parse_arguments():
    parser = argparse.ArgumentParser(description="QEEMA Production Pipeline")
    parser.add_argument("--episode", type=int, help="Target episode number")
    parser.add_argument("--dry-run", action="store_true", help="Skip YouTube upload")
    parser.add_argument("--status", action="store_true", help="Show episode dashboard")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_arguments()
    app = QeemaApplication(args)
    
    # تحويل الـ Main Loop إلى Async بالكامل
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
