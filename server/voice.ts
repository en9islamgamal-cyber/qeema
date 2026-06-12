/**
 * QEEMA — Voice Service (ElevenLabs)
 * - يحوّل نص الشرح لصوت بصوتك (Multilingual v2).
 * - يشيل التشكيل قبل الإرسال (التشكيل بيضرّ النطق).
 * - يرجّع مسار mp3 ومدته. يفشل بصوت عالٍ.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { ELEVENLABS } from './config.ts';
import { normalizeArabicForTts } from './arabicTtsNormalizer.ts';

const execFileAsync = promisify(execFile);

/** إزالة التشكيل (الحركات) والتطويل عشان نطق أنضف. */
export function stripTashkeel(text: string): string {
  return text
    .replace(/[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED]/g, '')
    .replace(/\u0640/g, '') // تطويل
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * قاموس نطق مصري: بنصلّح فقط كلمات الموديل بينطقها غلط.
 * "قيمة" تتنطق بالقاف (qeema) زي اسم القناة — فمش بنغيّرها.
 * ضيف أي كلمة تلاقيها متنطقة غلط هنا [الكلمة, النطق الصح].
 * ملاحظة: مابنعملش استبدال شامل للقاف عشان مانخربش كلمات دينية زي "القرآن".
 */
const PRONUNCIATION: [RegExp, string][] = [
  // "قيمة" تتنطق بالقاف (qeema) — مش بنغيّرها.
  // تصحيح نطق "قرأ" المصري (بدون همزة):
  [/نقرأها|نقرأها/g, 'نقراها'],
  [/نقرأ/g, 'نقرا'],
  [/اقرأ/g, 'اقرا'],
  [/يقرأ/g, 'يقرا'],
  [/تقرأ/g, 'تقرا'],
  [/قرأنا/g, 'قرانا'],
  // تعبيرات ودودة بالعامية:
  [/أصدقائي/g, 'صحابي'],
  [/يا أصدقاء/g, 'يا صحاب'],
  [/إزيكوا|ازيكوا|إزيكم|ازيكم/g, 'إزّايّكوا'],
];

function applyPronunciation(text: string): string {
  let t = text;
  for (const [re, rep] of PRONUNCIATION) t = t.replace(re, rep);
  return t;
}

async function ffprobeDuration(file: string): Promise<number> {
  const { stdout } = await execFileAsync('ffprobe', [
    '-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1', file,
  ]);
  const sec = parseFloat(stdout.trim());
  if (!isFinite(sec) || sec <= 0) throw new Error(`[voice] مدة غير صالحة لـ ${file}`);
  return sec;
}

/**
 * يولّد مقطع صوت واحد من نص.
 * @param opts.raw   لو true: نص قرآني — يُحافَظ على التشكيل ومفيش قاموس نطق (يتقال زي ما هو).
 * @param opts.tempo سرعة (atempo): 0.95 = أبطأ شوية، 1.05 = أسرع شوية. الافتراضي 1.0.
 * @returns { filePath, durationSeconds }
 */
export async function synthesize(
  text: string,
  outPath: string,
  opts: { raw?: boolean; tempo?: number; retries?: number } = {}
): Promise<{ filePath: string; durationSeconds: number }> {
  const retries = opts.retries ?? 3;
  const tempo = opts.tempo ?? 1.0;
  // قرآن: نحافظ على التشكيل ومفيش قاموس نطق. غير كده: عامية بدون تشكيل + قاموس.
  // وفي الحالتين: تطبيع اللام الشمسية ولفظ الجلالة قبل الإرسال.
  const base = opts.raw ? text.replace(/\s+/g, ' ').trim() : applyPronunciation(stripTashkeel(text));
  const clean = normalizeArabicForTts(base);
  if (!clean) throw new Error('[voice] نص فاضي.');

  const url = `https://api.elevenlabs.io/v1/text-to-speech/${ELEVENLABS.voiceId}?output_format=mp3_44100_128`;
  const body = JSON.stringify({
    text: clean,
    model_id: ELEVENLABS.modelId,
    voice_settings: {
      stability: ELEVENLABS.stability,
      similarity_boost: ELEVENLABS.similarityBoost,
      style: ELEVENLABS.style,
      use_speaker_boost: ELEVENLABS.useSpeakerBoost,
    },
  });

  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'xi-api-key': ELEVENLABS.apiKey(), 'Content-Type': 'application/json', accept: 'audio/mpeg' },
        body,
      });
      if (!res.ok) {
        const errText = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${errText.slice(0, 300)}`);
      }
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length < 500) throw new Error(`صوت صغير/فاسد (${buf.length}B)`);
      fs.mkdirSync(path.dirname(outPath), { recursive: true });

      if (tempo && Math.abs(tempo - 1.0) > 0.001) {
        // اكتب مؤقت ثم طبّق السرعة
        const raw = outPath + '.raw.mp3';
        fs.writeFileSync(raw, buf);
        await execFileAsync('ffmpeg', ['-y', '-i', raw, '-filter:a', `atempo=${tempo}`, '-c:a', 'libmp3lame', '-q:a', '2', outPath]);
        try { fs.unlinkSync(raw); } catch {}
      } else {
        fs.writeFileSync(outPath, buf);
      }

      const durationSeconds = await ffprobeDuration(outPath);
      return { filePath: outPath, durationSeconds };
    } catch (err) {
      lastErr = err;
      console.warn(`[voice] محاولة ${attempt}/${retries} فشلت: ${String((err as Error)?.message || err)}`);
      if (attempt < retries) await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
  throw new Error(`[voice] فشل توليد الصوت بعد ${retries} محاولات: ${String((lastErr as Error)?.message || lastErr)}`);
}

/** يدمج عدة ملفات صوت في ملف واحد (للانترو الثابت + المتغيّر). */
export async function concatAudio(parts: string[], outPath: string): Promise<string> {
  const fs = await import('fs');
  const path = await import('path');
  const listPath = path.join(path.dirname(outPath), 'audio_concat.txt');
  fs.writeFileSync(listPath, parts.map((f) => `file '${path.resolve(f).replace(/'/g, "'\\''")}'`).join('\n'));
  await execFileAsync('ffmpeg', ['-y', '-f', 'concat', '-safe', '0', '-i', listPath, '-c:a', 'libmp3lame', '-q:a', '2', outPath]);
  return outPath;
}
 