/**
 * QEEMA — Video Assembly (FFmpeg حقيقي)
 * يبني الخط الزمني:
 *   مقدمة (صورة كاملة) ← تلاوة 1 (كاملة) ← [زوم على كل فكرة + شرحها] ← ختام (كاملة)
 *   ← تلاوة 2 (كاملة) ← الأوترو.
 * + واترمارك اللوجو + تعليقات عربية محروقة (ASS/libass).
 * يتطلّب ffmpeg/ffprobe وخط عربي (Noto Naskh Arabic) مركّبين.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { VIDEO, LOGO_PATH, OUTRO_PATH, ARABIC_FONT } from './config.ts';

const execFileAsync = promisify(execFile);
const W = VIDEO.width, H = VIDEO.height, FPS = VIDEO.fps;

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
async function hasAudio(file: string): Promise<boolean> {
  try {
    const { stdout } = await execFileAsync('ffprobe', [
      '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
      '-of', 'csv=p=0', file,
    ]);
    return stdout.trim().length > 0;
  } catch {
    return false;
  }
}

/** تخطيط الخلايا حسب العدد. كل خلية 16:9 (نصف العرض × نصف الطول) عشان الزوم يملأها بـ zoom=2. */
export function cellLayout(n: number): { x: number; y: number; w: number; h: number }[] {
  const cw = Math.floor(W / 2), ch = Math.floor(H / 2);
  if (n <= 3) {
    // 2 فوق + 1 تحت في النص
    return [
      { x: 0, y: 0, w: cw, h: ch },
      { x: cw, y: 0, w: cw, h: ch },
      { x: Math.floor(W / 4), y: ch, w: cw, h: ch },
    ].slice(0, n);
  }
  // 4: شبكة 2×2
  return [
    { x: 0, y: 0, w: cw, h: ch },
    { x: cw, y: 0, w: cw, h: ch },
    { x: 0, y: ch, w: cw, h: ch },
    { x: cw, y: ch, w: cw, h: ch },
  ];
}

/** يبني الصورة الكاملة من الاسكتشات على خلفية بيضا (تخطيط متأقلم 3 أو 4). */
export async function buildGrid(sketchPaths: string[], workDir: string): Promise<string> {
  const n = sketchPaths.length;
  const layout = cellLayout(n);
  const inputs: string[] = ['-f', 'lavfi', '-i', `color=white:s=${W}x${H}`];
  sketchPaths.forEach((p) => inputs.push('-i', p));

  const parts: string[] = [];
  sketchPaths.forEach((_, i) => {
    parts.push(`[${i + 1}:v]scale=${layout[i].w}:${layout[i].h}:force_original_aspect_ratio=increase,crop=${layout[i].w}:${layout[i].h},setsar=1[s${i}]`);
  });
  // overlay متسلسل فوق الخلفية البيضا
  let last = '[0:v]';
  sketchPaths.forEach((_, i) => {
    const out = i === sketchPaths.length - 1 ? '[grid]' : `[o${i}]`;
    parts.push(`${last}[s${i}]overlay=${layout[i].x}:${layout[i].y}${out}`);
    last = `[o${i}]`;
  });

  const grid = path.join(workDir, 'grid.png');
  await ff([...inputs, '-filter_complex', parts.join(';'), '-map', '[grid]', '-frames:v', '1', grid]);
  return grid;
}

/** كتابة ملف ASS لتعليق عربي واحد يغطّي مدة المقطع. */
function writeAss(text: string, workDir: string, tag: string): string {
  const safe = text.replace(/\n/g, ' ').replace(/[{}]/g, '');
  const ass = `[Script Info]
ScriptType: v4.00+
PlayResX: ${W}
PlayResY: ${H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Q,${ARABIC_FONT},58,&H00FFFFFF,&H00303030,&H90000000,1,3,2,2,60,60,70

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.00,9:59:59.00,Q,,${safe}
`;
  const p = path.join(workDir, `cap_${tag}.ass`);
  fs.writeFileSync(p, ass, 'utf-8');
  return p;
}

const escFilter = (p: string) => p.replace(/\\/g, '/').replace(/:/g, '\\:').replace(/'/g, "\\'");

interface Rect { x: number; y: number; w: number; h: number; }
interface ClipOpts {
  focus?: Rect;            // الخلية اللي نعمل عليها زوم
  caption?: string;        // تعليق عربي محروق
}

/** يصنع مقطع mp4 من صورة + صوت (بطول الصوت)، مع واترمارك وزوم وتعليق اختياري. */
async function makeClip(
  visual: string,
  audio: string,
  outPath: string,
  workDir: string,
  tag: string,
  opts: ClipOpts = {}
): Promise<string> {
  const dur = await ffprobeDuration(audio);
  if (dur <= 0) throw new Error(`[video] صوت بلا مدة: ${audio}`);
  const frames = Math.ceil(dur * FPS);

  const chain: string[] = [];
  // 1) القاعدة:
  //    - فكرة: زوم إن على الخلية (سريع) ← ثبات ← زوم أوت للصورة الكاملة في الآخر.
  //    - غير كده: الصورة الكاملة ثابتة.
  if (opts.focus) {
    const cx = opts.focus.x + opts.focus.w / 2;   // مركز الخلية في الشبكة
    const cy = opts.focus.y + opts.focus.h / 2;
    let inF = Math.round(1.2 * FPS);
    let outF = Math.round(1.0 * FPS);
    if (frames < inF + outF + FPS) { inF = Math.round(0.8 * FPS); outF = Math.round(0.6 * FPS); }
    const outStart = Math.max(inF + 1, frames - outF);
    const z = `if(lt(on,${inF}),1+on/${inF},if(lt(on,${outStart}),2,max(1,2-(on-${outStart})/${outF})))`;
    const x = `max(0\\,min(${cx}-(iw/zoom/2)\\,iw-iw/zoom))`;
    const y = `max(0\\,min(${cy}-(ih/zoom/2)\\,ih-ih/zoom))`;
    chain.push(
      `[0:v]scale=${W}:${H},setsar=1,` +
      `zoompan=z='${z}':x='${x}':y='${y}':d=1:s=${W}x${H}:fps=${FPS}[v0]`
    );
  } else {
    chain.push(`[0:v]scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1,fps=${FPS}[v0]`);
  }
  // 2) واترمارك اللوجو (input #2) — أكبر
  chain.push(`[2:v]scale=300:-1[lg]`);
  chain.push(`[v0][lg]overlay=W-w-40:H-h-40[v1]`);
  // 3) تعليق عربي (libass) لو موجود
  let lastV = '[v1]';
  if (opts.caption && opts.caption.trim()) {
    const assPath = writeAss(opts.caption.trim(), workDir, tag);
    chain.push(`[v1]subtitles='${escFilter(assPath)}'[v2]`);
    lastV = '[v2]';
  }

  await ff([
    '-framerate', String(FPS), '-loop', '1', '-t', String(dur), '-i', visual,
    '-i', audio,
    '-loop', '1', '-t', String(dur), '-i', LOGO_PATH,
    '-filter_complex', chain.join(';'),
    '-map', lastV, '-map', '1:a',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
    '-c:a', 'aac', '-ar', '44100', '-b:a', '192k',
    '-shortest', outPath,
  ]);
  return outPath;
}

/** يعيد ترميز الأوترو لنفس مواصفات المقاطع (مع صوت صامت لو ملوش صوت). */
async function normalizeOutro(workDir: string): Promise<string | null> {
  if (!fs.existsSync(OUTRO_PATH)) return null;
  const out = path.join(workDir, 'outro_norm.mp4');
  const audio = await hasAudio(OUTRO_PATH);

  // خلفية مموّهة من الفيديو نفسه تملا الجوانب (بدل الأسود) + الفيديو في النص
  const vfilter =
    `[0:v]split=2[bg][fg];` +
    `[bg]scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},boxblur=22:4,eq=brightness=-0.06,setsar=1[bgb];` +
    `[fg]scale=${W}:${H}:force_original_aspect_ratio=decrease,setsar=1[fgs];` +
    `[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=${FPS}[v]`;

  const args = audio
    ? [
        '-i', OUTRO_PATH,
        '-filter_complex', vfilter,
        '-map', '[v]', '-map', '0:a',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
        '-c:a', 'aac', '-ar', '44100', '-b:a', '192k', out,
      ]
    : [
        '-i', OUTRO_PATH,
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
        '-filter_complex', vfilter,
        '-map', '[v]', '-map', '1:a',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
        '-c:a', 'aac', '-ar', '44100', '-b:a', '192k', '-shortest', out,
      ];
  await ff(args);
  return out;
}

async function concat(clips: string[], outPath: string, workDir: string): Promise<void> {
  const list = path.join(workDir, 'concat_clips.txt');
  fs.writeFileSync(list, clips.map((c) => `file '${path.resolve(c).replace(/'/g, "'\\''")}'`).join('\n'));
  // إعادة ترميز عند الدمج لضمان توافق كل المقاطع
  await ff(['-f', 'concat', '-safe', '0', '-i', list, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-c:a', 'aac', '-ar', '44100', outPath]);
}

export interface AssemblyInput {
  workDir: string;
  gridImage: string;
  recitationPath: string;
  introAudio: string;
  closingAudio: string;
  ideas: { focus: Rect; audioPath: string; caption: string }[];
  introCaption: string; // عادةً عنوان الحلقة
}

/** يجمّع الحلقة كاملة ويرجّع مسار الفيديو النهائي. */
export async function assembleEpisode(input: AssemblyInput): Promise<string> {
  const { workDir, gridImage, recitationPath, introAudio, closingAudio, ideas } = input;
  const clips: string[] = [];

  console.log('[video] مقطع المقدمة');
  clips.push(await makeClip(gridImage, introAudio, path.join(workDir, 'c_intro.mp4'), workDir, 'intro', { caption: input.introCaption }));

  console.log('[video] التلاوة الأولى');
  clips.push(await makeClip(gridImage, recitationPath, path.join(workDir, 'c_recite1.mp4'), workDir, 'recite1', {}));

  for (let i = 0; i < ideas.length; i++) {
    console.log(`[video] فكرة ${i + 1}/${ideas.length}`);
    clips.push(await makeClip(gridImage, ideas[i].audioPath, path.join(workDir, `c_idea${i}.mp4`), workDir, `idea${i}`, {
      focus: ideas[i].focus, caption: ideas[i].caption,
    }));
  }

  console.log('[video] الختام');
  clips.push(await makeClip(gridImage, closingAudio, path.join(workDir, 'c_closing.mp4'), workDir, 'closing', {}));

  const outro = await normalizeOutro(workDir);
  if (outro) clips.push(outro);
  else console.warn('[video] تحذير: مفيش outro.mp4 — هيتمّ التجميع بدون أوترو.');

  // تشخيص: تأكد إن كل مقطع موجود وله مدة
  const labels = ['intro', 'recite1', ...ideas.map((_, i) => `idea${i}`), 'closing', ...(outro ? ['outro'] : [])];
  for (let i = 0; i < clips.length; i++) {
    if (!fs.existsSync(clips[i]) || fs.statSync(clips[i]).size < 1000) {
      throw new Error(`[video] المقطع "${labels[i]}" مفقود/فاضي: ${clips[i]}`);
    }
    const d = await ffprobeDuration(clips[i]);
    console.log(`[video] مقطع ${labels[i]}: ${d.toFixed(1)}s`);
    if (d < 0.3) throw new Error(`[video] المقطع "${labels[i]}" مدته شبه صفر (${d}s) — وقفنا عشان مايطلعش فيديو ناقص.`);
  }

  const finalPath = path.join(workDir, 'final.mp4');
  console.log(`[video] دمج ${clips.length} مقطع -> final.mp4`);
  await concat(clips, finalPath, workDir);
  if (!fs.existsSync(finalPath) || fs.statSync(finalPath).size < 10000) {
    throw new Error('[video] الفيديو النهائي فاضي/فاسد.');
  }
  console.log(`[video] جاهز: ${finalPath}`);
  return finalPath;
}
