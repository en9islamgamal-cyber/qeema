/**
 * QEEMA — Recitation Service
 * تجيب آيات السورة من everyayah (الحصري المعلّم آية-آية)، تدمجها، وتطبّق atempo.
 * تفشل بصوت عالٍ لو أي آية ماتنزلتش. تتطلّب ffmpeg/ffprobe في الـ PATH.
 *
 * ⚠️ إصلاح مشكلة قص أول/آخر الآية:
 *   لزق ملفات MP3 مباشرة (concat demuxer) بيقص حروف في حدود كل ملف بسبب
 *   الـ encoder delay/padding بتاع MP3. الحل: نفكّ كل آية لـ WAV (PCM نظيف)،
 *   نحط سكتة أمان أول/آخر كل آية، نلزّقهم WAV (مفيش قص)، وبعدين atempo + mp3.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { SurahInput } from './prompts.ts';
import { EVERYAYAH_BASE, RECITER, RECITATION_TEMPO } from './config.ts';

const execFileAsync = promisify(execFile);

/* ============================================================
 * إعدادات السكتات (بالمللي ثانية) — قابلة للتعديل من البيئة.
 * ========================================================== */
const num = (k: string, d: number) => {
  const v = process.env[k];
  const n = v ? parseInt(v, 10) : NaN;
  return Number.isFinite(n) && n >= 0 ? n : d;
};
const AYAH_LEAD_MS = num('AYAH_LEAD_MS', 80);   // سكوت أمان قبل كل آية (يحمي أول حرف)
const AYAH_TAIL_MS = num('AYAH_TAIL_MS', 130);  // جاب طبيعي بعد كل آية (بين الآيات)
const HEAD_MS = num('RECITATION_HEAD_MS', 150);  // سكوت في أول التلاوة كلها
const END_GAP_MS = num('RECITATION_END_GAP_MS', 350); // جاب بعد التلاوة وقبل الشرح

/** عدد آيات كل سورة (عدّ حفص) للفاتحة + جزء عمّ. */
const AYAH_COUNTS: Record<number, number> = {
  1: 7,
  78: 40, 79: 46, 80: 42, 81: 29, 82: 19, 83: 36, 84: 25, 85: 22, 86: 17,
  87: 19, 88: 26, 89: 30, 90: 20, 91: 15, 92: 21, 93: 11, 94: 8, 95: 8,
  96: 19, 97: 5, 98: 8, 99: 8, 100: 11, 101: 11, 102: 8, 103: 3, 104: 9,
  105: 5, 106: 4, 107: 7, 108: 3, 109: 6, 110: 3, 111: 5, 112: 4, 113: 5,
  114: 6,
};

export function getAyahCount(surahNumber: number): number {
  const c = AYAH_COUNTS[surahNumber];
  if (!c) throw new Error(`[reciter] عدد آيات غير معروف للسورة ${surahNumber}. أضِفها لـ AYAH_COUNTS.`);
  return c;
}

const pad3 = (n: number) => String(n).padStart(3, '0');
const ayahUrl = (s: number, a: number) => `${EVERYAYAH_BASE}/${RECITER}/${pad3(s)}${pad3(a)}.mp3`;
const escFile = (p: string) => path.resolve(p).replace(/'/g, "'\\''");

async function downloadAyah(s: number, a: number, dest: string, retries = 3): Promise<void> {
  if (fs.existsSync(dest) && fs.statSync(dest).size > 1000) return; // كاش بسيط
  const url = ayahUrl(s, a);
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status} :: ${url}`);
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length < 1000) throw new Error(`ملف صغير/فاسد (${buf.length}B) :: ${url}`);
      fs.writeFileSync(dest, buf);
      return;
    } catch (err) {
      lastErr = err;
      if (attempt < retries) await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
  throw new Error(`[reciter] فشل تنزيل آية ${a}/سورة ${s} بعد ${retries} محاولات: ${String((lastErr as Error)?.message || lastErr)}`);
}

async function ffprobeDuration(file: string): Promise<number> {
  const { stdout } = await execFileAsync('ffprobe', [
    '-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1', file,
  ]);
  const sec = parseFloat(stdout.trim());
  if (!isFinite(sec) || sec <= 0) throw new Error(`[reciter] مدة غير صالحة لـ ${file}`);
  return sec;
}

/**
 * يفكّ آية MP3 -> WAV (44.1k/stereo) مع سكتة أمان قبلها وبعدها.
 * ده اللي بيمنع قص أول/آخر الآية عند اللزق.
 */
async function ayahToPaddedWav(srcMp3: string, destWav: string): Promise<void> {
  await execFileAsync('ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'error',
    '-i', srcMp3, '-ar', '44100', '-ac', '2',
    '-af', `adelay=${AYAH_LEAD_MS}:all=1,apad=pad_dur=${(AYAH_TAIL_MS / 1000).toFixed(3)}`,
    '-c:a', 'pcm_s16le', destWav,
  ]);
}

/**
 * يدمج قائمة WAV مبطّنة في تلاوة mp3 نهائية:
 * سكوت أول + جاب نهاية (قبل الشرح) + atempo.
 */
async function joinWavsToMp3(wavs: string[], outMp3: string): Promise<void> {
  const dir = path.dirname(outMp3);
  const listPath = path.join(dir, `recite_list_${path.basename(outMp3)}.txt`);
  fs.writeFileSync(listPath, wavs.map((f) => `file '${escFile(f)}'`).join('\n'));

  const joined = outMp3 + '.joined.wav';
  await execFileAsync('ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'error',
    '-f', 'concat', '-safe', '0', '-i', listPath,
    '-c:a', 'pcm_s16le', joined,
  ]);

  await execFileAsync('ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'error',
    '-i', joined,
    '-af', `adelay=${HEAD_MS}:all=1,apad=pad_dur=${(END_GAP_MS / 1000).toFixed(3)},atempo=${RECITATION_TEMPO}`,
    '-c:a', 'libmp3lame', '-q:a', '2', outMp3,
  ]);

  try { fs.unlinkSync(joined); } catch {}
  if (!fs.existsSync(outMp3) || fs.statSync(outMp3).size < 1000) {
    throw new Error(`[reciter] فشل إنتاج التلاوة على ${outMp3}`);
  }
}

/**
 * تلاوة نطاق آيات السورة كله (الاستخدام الحالي).
 * @returns { filePath, durationSeconds }
 */
export async function fetchRecitation(
  surah: SurahInput,
  workDir: string
): Promise<{ filePath: string; durationSeconds: number }> {
  const total = getAyahCount(surah.surahNumber);
  const start = Math.max(1, surah.ayahStart || 1);
  const end = surah.ayahEnd && surah.ayahEnd > 0 ? Math.min(surah.ayahEnd, total) : total;
  if (start > end) throw new Error(`[reciter] نطاق آيات غير صالح ${start}-${end} (السورة ${total} آية)`);

  const ayatDir = path.join(workDir, 'ayat');
  fs.mkdirSync(ayatDir, { recursive: true });
  console.log(`[reciter] سورة ${surah.surahName}: تنزيل الآيات ${start}-${end} (${RECITER})`);

  const wavs: string[] = [];
  for (let a = start; a <= end; a++) {
    const mp3 = path.join(ayatDir, `${pad3(surah.surahNumber)}${pad3(a)}.mp3`);
    const wav = path.join(ayatDir, `${pad3(surah.surahNumber)}${pad3(a)}.wav`);
    await downloadAyah(surah.surahNumber, a, mp3);
    await ayahToPaddedWav(mp3, wav);
    wavs.push(wav);
  }

  const out = path.join(workDir, 'recitation.mp3');
  await joinWavsToMp3(wavs, out);

  const durationSeconds = await ffprobeDuration(out);
  console.log(`[reciter] تمّت التلاوة: ${out} (${durationSeconds.toFixed(1)}s)`);
  return { filePath: out, durationSeconds };
}

/**
 * تلاوة آية (أو نطاق صغير) واحدة فقط — للاستخدام في بنية "آية تتقال ثم تتشرح".
 * بنفس الحماية من القص والجاب قبل الشرح.
 * @param ayahStart أول آية (1-based)
 * @param ayahEnd   آخر آية (لو مش متبعتة = نفس ayahStart)
 * @returns { filePath, durationSeconds }
 */
export async function fetchAyahClip(
  surahNumber: number,
  ayahStart: number,
  workDir: string,
  ayahEnd?: number
): Promise<{ filePath: string; durationSeconds: number }> {
  const total = getAyahCount(surahNumber);
  const start = Math.max(1, ayahStart);
  const end = Math.min(ayahEnd && ayahEnd > 0 ? ayahEnd : start, total);
  if (start > end) throw new Error(`[reciter] نطاق آية غير صالح ${start}-${end} (السورة ${surahNumber})`);

  const ayatDir = path.join(workDir, 'ayat');
  fs.mkdirSync(ayatDir, { recursive: true });

  const wavs: string[] = [];
  for (let a = start; a <= end; a++) {
    const mp3 = path.join(ayatDir, `${pad3(surahNumber)}${pad3(a)}.mp3`);
    const wav = path.join(ayatDir, `${pad3(surahNumber)}${pad3(a)}.wav`);
    await downloadAyah(surahNumber, a, mp3);
    await ayahToPaddedWav(mp3, wav);
    wavs.push(wav);
  }

  const tag = end > start ? `${pad3(start)}-${pad3(end)}` : pad3(start);
  const out = path.join(workDir, `ayah_${pad3(surahNumber)}_${tag}.mp3`);
  await joinWavsToMp3(wavs, out);

  const durationSeconds = await ffprobeDuration(out);
  return { filePath: out, durationSeconds };
}
