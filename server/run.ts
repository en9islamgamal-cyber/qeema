/**
 * QEEMA — CLI / CI entry point
 * يستبدل سكريبت الـ -e القديم في الـ workflow (أنضف + بيضمن exit code صح).
 *   npx tsx server/run.ts            -> يشغّل الحلقة التالية المجدولة
 *   npx tsx server/run.ts 7          -> يشغّل الحلقة رقم 7 إجباريًا
 */
import { runEpisode, runNextScheduled } from './orchestrator.ts';

async function main() {
  const arg = process.argv[2]?.trim();
  if (arg) {
    console.log(`[run] تشغيل إجباري للحلقة: ${arg}`);
    await runEpisode(arg);
  } else {
    console.log('[run] فحص الجدول وتشغيل الحلقة التالية…');
    await runNextScheduled();
  }
}

main()
  .then(() => {
    console.log('[run] انتهى بنجاح.');
    process.exit(0);
  })
  .catch((err) => {
    console.error('[run] انتهى بخطأ:', err?.message || err);
    process.exit(1);
  });
