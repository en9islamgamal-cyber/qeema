/**
 * QEEMA — Shorts Generator (FFmpeg حقيقي، صفر API)
 * ------------------------------------------------------------------
 * بيعيد استخدام أصول الحلقة المتولّدة فعلاً (sketch{i}.png + narr_idea{i}.mp3 + caption)
 * عشان يطلّع شورتس عمودية 1080×1920 — من غير ما يلمس Gemini / ElevenLabs / التلاوة تاني.
 * يتشغّل في نفس الـ run بعد assembleEpisode (الأصول لسه على الديسك).
 *
 * التخطيط (مثالي للسكتش السكوير):
 *   ورق كريمي 1080×1920 → السكتش 1080×1080 في النص (صفر قص)
 *   شريط علوي: اسم السورة + رقم الآية   |   شريط سفلي: تعليق عربي
 *   + واترمارك اللوجو + زوم خفيف (حياة) + نص عربي محروق (ASS/libass).
 *
 * الفلسفة: الشورتس "بونص" مش الناتج الأساسي — فلو فشل شورت، نسجّل ونكمّل،
 * من غير ما نوقّع الحلقة اللي اترفعت أصلاً.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { VIDEO, LOGO_PATH, ARABIC_FONT } from './config.ts';

const execFileAsync = promisify(execFile);

/* أبعاد الشورت العمودي (مستقلة عن VIDEO الأفقي) */
const SW = 1080;             // العرض
const SH = 1920;             // الطول
const FPS = VIDEO.fps;       // نفس معدّل الإطارات بتاع الحلقة
const SQ = SW;               // السكتش سكوير → 1080×1080
const TOP = Math.floor((SH - SQ) / 2);   // 420 (شريط علوي/سفلي)
const PAPER = '0xFBF7EE';    // لون الورق الكريمي (نفس روح خلفية الحلقة)

async function ff(args: string[]): Promise<void> {
  await execFileAsync('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', ...args], { maxBuffer: 1024 * 1024 * 64 });
}
async function ffprobeDuration(file: string): Promise<number> {
  const { stdout } = await execFileAsync('ffprobe', [
    '-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1', file,
  ]);
  return parseFloat(stdout.trim()) || 0;
}

const escFilter = (p: string) => p.replace(/\\/g, '/').replace(/:/g, '\\:').replace(/'/g, "\\'");
const sanitize = (t: string) => t.replace(/\n/g, ' ').replace(/[{}]/g, '').trim();

/* أرقام عربية-هندية للهيدر (١ بدل 1) */
function arabicDigits(n: number): string {
  const map = '٠١٢٣٤٥٦٧٨٩';
  return String(n).replace(/\d/g, (d) => map[+d]);
}
function ayahLabel(start: number, end: number | null): string {
  if (end && end !== start) return `الآيات ${arabicDigits(start)}–${arabicDigits(end)}`;
  return `الآية ${arabicDigits(start)}`;
}

/** ملف ASS: هيدر (أعلى: سورة + رقم آية) + نص الآية بالتشكيل (أسفل، يلتفّ لو طويل). */
function writeAss(header: string, ayahText: string, workDir: string, tag: string): string {
  const events: string[] = [
    `Dialogue: 0,0:00:00.00,9:59:59.00,Header,,${sanitize(header)}`,
  ];
  const ayah = sanitize(ayahText);
  if (ayah) events.push(`Dialogue: 0,0:00:00.00,9:59:59.00,Ayah,,${ayah}`);

  const ass =
`[Script Info]
ScriptType: v4.00+
PlayResX: ${SW}
PlayResY: ${SH}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Header,${ARABIC_FONT},50,&H00505050,&H00FFFFFF,&H00000000,1,2,0,8,60,60,120
Style: Ayah,${ARABIC_FONT},54,&H0033E0FF,&H00202020,&H90000000,1,4,2,2,90,90,150

[Events]
Format: Layer, Start, End, Style, Text
${events.join('\n')}
`;
  const p = path.join(workDir, `short_cap_${tag}.ass`);
  fs.writeFileSync(p, ass, 'utf-8');
  return p;
}

export interface ShortInput {
  sketchPath: string;        // sketch{i}.png (سكوير)
  audioPath: string;         // narr_idea{i}.mp3 (آية + شرح) — جاهز من الكاش
  ayahText: string;          // نص الآية بالتشكيل (السطر السفلي) — من ayahRangeForTts
  surahName: string;         // ep.surahName
  ayahStart: number;
  ayahEnd: number | null;
}

/** يبني شورت واحد عمودي ويرجّع مساره. صفر استدعاء API. */
export async function buildShort(s: ShortInput, workDir: string, tag: string): Promise<string> {
  if (!fs.existsSync(s.sketchPath)) throw new Error(`[shorts] سكتش مفقود: ${s.sketchPath}`);
  if (!fs.existsSync(s.audioPath)) throw new Error(`[shorts] أوديو مفقود: ${s.audioPath}`);

  const dur = await ffprobeDuration(s.audioPath);
  if (dur <= 0) throw new Error(`[shorts] أوديو بلا مدة: ${s.audioPath}`);
  const frames = Math.max(1, Math.ceil(dur * FPS));

  const header = `سورة ${s.surahName} — ${ayahLabel(s.ayahStart, s.ayahEnd)}`;
  const assPath = writeAss(header, s.ayahText || '', workDir, tag);
  const hasLogo = fs.existsSync(LOGO_PATH);

  // ملاحظة: من غير فواصل (,) جوّه تعبيرات zoompan عشان نتجنّب تهريب الفلتر.
  const chain: string[] = [
    `[1:v]scale=${SQ}:${SQ}:force_original_aspect_ratio=increase,crop=${SQ}:${SQ},setsar=1,` +
      `zoompan=z='1+0.08*on/${frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=${SQ}x${SQ}:fps=${FPS}[sk]`,
    `[0:v][sk]overlay=0:${TOP}[base]`,
  ];

  let lastV = '[base]';
  if (hasLogo) {
    chain.push(`[2:v]scale=170:-1[lg]`);
    chain.push(`[base][lg]overlay=W-w-34:34[wm]`);
    lastV = '[wm]';
  }
  chain.push(`${lastV}subtitles='${escFilter(assPath)}'[v]`);

  const outPath = path.join(workDir, `short_${tag}.mp4`);
  const inputs: string[] = [
    '-f', 'lavfi', '-t', String(dur), '-i', `color=c=${PAPER}:s=${SW}x${SH}:r=${FPS}`,  // 0: الورق
    '-loop', '1', '-framerate', String(FPS), '-t', String(dur), '-i', s.sketchPath,      // 1: السكتش
  ];
  if (hasLogo) inputs.push('-loop', '1', '-t', String(dur), '-i', LOGO_PATH);            // 2: اللوجو
  const audioIdx = hasLogo ? 3 : 2;
  inputs.push('-i', s.audioPath);                                                         // الأوديو

  await ff([
    ...inputs,
    '-filter_complex', chain.join(';'),
    '-map', '[v]', '-map', `${audioIdx}:a`,
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
    '-c:a', 'aac', '-ar', '44100', '-b:a', '192k',
    '-shortest', outPath,
  ]);

  if (!fs.existsSync(outPath) || fs.statSync(outPath).size < 5000) {
    throw new Error(`[shorts] الشورت طلع فاضي/فاسد: ${outPath}`);
  }
  return outPath;
}

/**
 * يطلّع شورت لكل فكرة. كل شورت مستقل: لو واحد فشل نسجّل ونكمّل.
 * يرجّع مسارات الشورتس اللي نجحت بس.
 */
export async function generateShorts(items: ShortInput[], workDir: string): Promise<string[]> {
  const out: string[] = [];
  for (let i = 0; i < items.length; i++) {
    try {
      const p = await buildShort(items[i], workDir, String(i));
      const d = await ffprobeDuration(p);
      console.log(`[shorts] شورت ${i + 1}/${items.length} جاهز (${d.toFixed(1)}s): ${path.basename(p)}`);
      out.push(p);
    } catch (err: any) {
      console.warn(`[shorts] فشل شورت ${i + 1}/${items.length}: ${String(err?.message || err)} — بنكمّل.`);
    }
  }
  console.log(`[shorts] الإجمالي: ${out.length}/${items.length} شورت بصفر استهلاك credits.`);
  return out;
}
