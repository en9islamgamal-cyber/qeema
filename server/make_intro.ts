/**
 * QEEMA — مولّد الانترو الثابت (لمرة واحدة)
 * بيولّد صوت المقدمة الثابت بصوت الراوية (ElevenLabs) ويحطّه في assets/intro.mp3
 *
 * البنية الزمنية:  [ثانيتين سكوت] + [الكلام] + [ثانيتين سكوت]
 * فالطول النهائي = 2 + مدة الكلام + 2  (الاسكربت بيطبعه — خلّي intro.mp4 بنفس الطول).
 *
 * بيتشغّل مرة واحدة بس، والناتج بيترفع على الريبو ويُعاد استخدامه كل حلقة
 * من غير أي استهلاك جديد لباقة ElevenLabs.
 *
 * نص الانترو مصدره واحد (FIXED_INTRO_TEXT في prompts.ts) عشان الـ LLM يعرفه
 * ويكمّل عليه في كل حلقة من غير ما يكرّره.
 *
 * شغّله مرة: npx tsx server/make_intro.ts
 */
import { synthesize } from './voice.ts';
import { FIXED_INTRO_TEXT } from './prompts.ts';
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

const LEAD = parseFloat(process.env.INTRO_LEAD_SECONDS || '2');  // سكوت البداية
const TAIL = parseFloat(process.env.INTRO_TAIL_SECONDS || '2');  // سكوت النهاية

async function main() {
  const assetsDir = path.join(process.cwd(), 'assets');
  fs.mkdirSync(assetsDir, { recursive: true });
  const raw = path.join(assetsDir, 'intro.raw.mp3');
  const out = path.join(assetsDir, 'intro.mp3');

  console.log('بولّد صوت الانترو الثابت…');
  const r = await synthesize(FIXED_INTRO_TEXT, raw, { tempo: 1.0 });
  console.log(`صوت الكلام: ${r.durationSeconds.toFixed(1)}s — بضيف ${LEAD}s سكوت أول و${TAIL}s آخر…`);

  // ثانيتين سكوت في الأول (adelay) + ثانيتين سكوت في الآخر (apad pad_dur).
  await execFileAsync('ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'error',
    '-i', raw,
    '-af', `adelay=${Math.round(LEAD * 1000)}:all=1,apad=pad_dur=${TAIL}`,
    '-c:a', 'libmp3lame', '-q:a', '2', out,
  ]);
  try { fs.unlinkSync(raw); } catch {}

  const total = LEAD + r.durationSeconds + TAIL;
  console.log(`✅ اتعمل: ${out}`);
  console.log(`⏱️  الطول الكلي ≈ ${total.toFixed(1)}s  → خلّي مدة assets/intro.mp4 بنفس الرقم ده تقريبًا.`);
  console.log('اسمعه! لو "قيمة" والنطق صح، ارفعه على الريبو في assets/intro.mp3.');
}
main().catch((e) => { console.error('فشل:', e?.message || e); process.exit(1); });
