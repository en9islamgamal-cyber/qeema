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

// ===== محرّك النطق المصري =====
// خريطة فصحى→مصري (كلمات وظيفية آمنة). مُطفأة افتراضيًا — شغّلها بـ EGY_DIALECT_MAP=true.
const DIALECT_MAP: Record<string, string> = {
  'هذا': 'ده', 'هذه': 'دي', 'هؤلاء': 'دول', 'ذلك': 'ده', 'تلك': 'دي',
  'الذي': 'اللي', 'التي': 'اللي', 'الذين': 'اللي',
  'ماذا': 'إيه', 'لماذا': 'ليه', 'كيف': 'إزاي', 'متى': 'إمتى', 'أين': 'فين',
  'الآن': 'دلوقتي', 'ليس': 'مش', 'ليست': 'مش',
  'يريد': 'عايز', 'نريد': 'عايزين', 'أريد': 'عايز',
  'كثيرا': 'كتير', 'جدا': 'قوي', 'أيضا': 'كمان', 'فقط': 'بس',
};

// كلمات دينية/فصحى ممنوع المسّ بيها نهائيًا (لا من الخريطة ولا القاموس).
const PROTECTED = new Set<string>([
  'الله', 'اللهم', 'لله', 'بالله', 'والله', 'تالله', 'الرحمن', 'الرحيم', 'القرآن', 'قرآن',
  'الكريم', 'الرسول', 'النبي', 'الآية', 'الآيات', 'سبحان', 'سبحانه', 'تعالى', 'الجنة', 'النار',
  'الصلاة', 'الزكاة', 'الذكر', 'ذكر', 'الحمد', 'بسم',
]);

function escapeRe(s: string): string { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// استبدال على مستوى الكلمة الكاملة فقط (مش جزء من كلمة).
function replaceWord(text: string, from: string, to: string): string {
  if (!from || PROTECTED.has(from)) return text;
  const re = new RegExp(`(^|[^\\p{L}\\p{N}_])(${escapeRe(from)})(?=$|[^\\p{L}\\p{N}_])`, 'gu');
  return text.replace(re, (_m, pre) => `${pre}${to}`);
}

// قاموس المستخدم: assets/pronunciation.json + متغيّر EGY_DICT (JSON). بيتحمّل مرة واحدة.
let _userDict: Record<string, string> | null = null;
function userDict(): Record<string, string> {
  if (_userDict) return _userDict;
  const d: Record<string, string> = {};
  try {
    const p = path.join(process.cwd(), 'assets', 'pronunciation.json');
    if (fs.existsSync(p)) Object.assign(d, JSON.parse(fs.readFileSync(p, 'utf8')));
  } catch { console.warn('[voice] تحذير: assets/pronunciation.json مش JSON صالح — اتساب.'); }
  try { if (process.env.EGY_DICT) Object.assign(d, JSON.parse(process.env.EGY_DICT)); } catch {}
  _userDict = d;
  const n = Object.keys(d).length;
  if (n) console.log(`[voice] قاموس النطق المصري: ${n} كلمة محمّلة.`);
  return d;
}

function applyPronunciation(text: string): string {
  let t = text;
  // ١) قواعد مدمجة (regex) — تصحيحات نطق ثابتة
  for (const [re, rep] of PRONUNCIATION) t = t.replace(re, rep);
  // ٢) خريطة فصحى→مصري (اختيارية، محمية للكلمات الدينية)
  if (process.env.EGY_DIALECT_MAP === 'true') {
    for (const [from, to] of Object.entries(DIALECT_MAP)) t = replaceWord(t, from, to);
  }
  // ٣) قاموس المستخدم (أعلى أولوية — للكلمات اللي بتطلع غلط؛ ممكن تحط نطقها بالتشكيل)
  for (const [from, to] of Object.entries(userDict())) t = replaceWord(t, from, to);
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
  // قرآن (raw): يُقرأ بنصّه وتشكيله الأصلي زي ما هو — ده أنضف نطق.
  // غير كده: عامية بدون تشكيل + قاموس نطق مصري بسيط بس.
  // ⚠️ ألغينا طبقة تطبيع اللام الشمسية/لفظ الجلالة لأنها كانت بتبوّظ النطق:
  //    كانت بتعيد كتابة "الرحمن"->"ارّحمن" و"الله"->"اللّاه" وElevenLabs بينطقها غلط.
  const clean = opts.raw ? text.replace(/\s+/g, ' ').trim() : applyPronunciation(stripTashkeel(text));
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

/** يدمج عدة ملفات صوت في ملف واحد (تلاوة + شرح / الانترو الثابت + المتغيّر). */
export async function concatAudio(parts: string[], outPath: string): Promise<string> {
  // ⚠️ الـ concat demuxer كان بيسكّت التلاوة عشان اختلاف القنوات (تلاوة ستيريو + شرح mono).
  // الحل: concat filter — نطبّع كل جزء (ستيريو 44100) قبل الدمج.
  const inputs: string[] = [];
  for (const p of parts) inputs.push('-i', p);

  let pre = '';
  let ins = '';
  parts.forEach((_, i) => {
    pre += `[${i}:a]aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo[a${i}];`;
    ins += `[a${i}]`;
  });
  const filter = `${pre}${ins}concat=n=${parts.length}:v=0:a=1[a]`;

  await execFileAsync('ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'error',
    ...inputs,
    '-filter_complex', filter,
    '-map', '[a]',
    '-c:a', 'libmp3lame', '-q:a', '2', outPath,
  ]);
  return outPath;
}
