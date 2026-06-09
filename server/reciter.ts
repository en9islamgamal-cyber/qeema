/**
 * QEEMA — Recitation Service
 * تجيب آيات السورة من everyayah (الحصري المعلّم آية-آية)، تدمجها، وتطبّق atempo.
 * تفشل بصوت عالٍ لو أي آية ماتنزلتش. تتطلّب ffmpeg/ffprobe في الـ PATH.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { SurahInput } from './prompts.ts';
import { EVERYAYAH_BASE, RECITER, RECITATION_TEMPO } from './config.ts';

const execFileAsync = promisify(execFile);

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

async function downloadAyah(s: number, a: number, dest: string, retries = 3): Promise<void> {
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

  const files: string[] = [];
  for (let a = start; a <= end; a++) {
    const dest = path.join(ayatDir, `${pad3(surah.surahNumber)}${pad3(a)}.mp3`);
    await downloadAyah(surah.surahNumber, a, dest);
    files.push(dest);
  }

  const listPath = path.join(ayatDir, 'concat.txt');
  fs.writeFileSync(listPath, files.map((f) => `file '${path.resolve(f).replace(/'/g, "'\\''")}'`).join('\n'));

  const out = path.join(workDir, 'recitation.mp3');
  await execFileAsync('ffmpeg', [
    '-y', '-f', 'concat', '-safe', '0', '-i', listPath,
    '-filter:a', `atempo=${RECITATION_TEMPO}`, '-c:a', 'libmp3lame', '-q:a', '2', out,
  ]);
  if (!fs.existsSync(out) || fs.statSync(out).size < 1000) throw new Error(`[reciter] فشل إنتاج التلاوة على ${out}`);

  const durationSeconds = await ffprobeDuration(out);
  console.log(`[reciter] تمّت التلاوة: ${out} (${durationSeconds.toFixed(1)}s)`);
  return { filePath: out, durationSeconds };
}
