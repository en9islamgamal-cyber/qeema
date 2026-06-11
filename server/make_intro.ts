/**
 * QEEMA — مولّد الانترو الثابت (لمرة واحدة)
 * بيولّد الجزء الثابت من المقدمة بصوت الراوية ويحطّه في assets/intro.mp3
 * شغّله مرة: npx tsx make_intro.ts
 * اسمع الناتج — لو "قيمة" والنطق صح، خلاص. لو لأ، عدّل INTRO_TEXT وأعد التشغيل.
 */
import { synthesize } from './server/voice.ts';
import * as path from 'path';

// الجزء الثابت من المقدمة (نطق "قيمة" بالقاف — نأكّده بالسمع)
const INTRO_TEXT =
  'إزّايّكوا يا صحابي! اتفضّلوا اركبوا معانا على سفينة قيمة... ' +
  'النهارده هنبحر في رحلة جديدة، نفهم فيها سوا تفسير سورة';

async function main() {
  const out = path.join(process.cwd(), 'assets', 'intro.mp3');
  console.log('بولّد الانترو الثابت…');
  const r = await synthesize(INTRO_TEXT, out);
  console.log(`✅ اتعمل: ${r.filePath} (${r.durationSeconds.toFixed(1)}s)`);
  console.log('اسمعه! لو "قيمة" والنطق صح، ارفعه على الريبو في assets/intro.mp3.');
  console.log('لو غلط، عدّل INTRO_TEXT في الملف ده وأعد التشغيل.');
}
main().catch((e) => { console.error('فشل:', e?.message || e); process.exit(1); });
