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

/** يبني شبكة 2×2 من الاسكتشات (الأماكن الفاضية تتملي باللوجو). */
export async function buildGrid(sketchPaths: string[], workDir: string): Promise<string> {
  const cell = { w: Math.floor(W / 2), h: Math.floor(H / 2) };
  const cells = [...sketchPaths];
  while (cells.length < 4) cells.push(LOGO_PATH); // ملء الفاضي باللوجو
  const inputs: string[] = [];
  cells.slice(0, 4).forEach((p) => inputs.push('-i', p));
  const scaled = cells
    .slice(0, 4)
    .map((_, i) => `[${i}:v]scale=${cell.w}:${cell.h}:force_original_aspect_ratio=increase,crop=${cell.w}:${cell.h},setsar=1[c${i}]`)
    .join(';');
  const grid = path.join(workDir, 'grid.png');
  await ff([
    ...inputs,
    '-filter_complex',
    `${scaled};[c0][c1]hstack=inputs=2[top];[c2][c3]hstack=inputs=2[bot];[top][bot]vstack=inputs=2[grid]`,
    '-map', '[grid]', '-frames:v', '1', grid,
  ]);
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

interface ClipOpts {
  quadrant?: number;       // 0..3 -> زوم على ربع الشبكة
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
  //    - فكرة: زوم إن على الركن (سريع) ← ثبات ← زوم أوت للصورة الكاملة في الآخر.
  //      كده كل مقطع يبدأ وينتهي على "الصورة الكاملة" فالقطع بين المقاطع غير محسوس.
  //    - غير كده: الصورة الكاملة ثابتة.
  if (opts.quadrant !== undefined) {
    const col = opts.quadrant % 2, row = Math.floor(opts.quadrant / 2);
    const cw = Math.floor(W / 2), ch = Math.floor(H / 2);
    const cx = col * cw + cw / 2;   // مركز الركن في الشبكة
    const cy = row * ch + ch / 2;
    let inF = Math.round(1.2 * FPS);   // مدة الزوم إن
    let outF = Math.round(1.0 * FPS);  // مدة الزوم أوت
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
  const args = audio
    ? [
        '-i', OUTRO_PATH,
        '-vf', `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=${FPS}`,
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
        '-c:a', 'aac', '-ar', '44100', '-b:a', '192k', out,
      ]
    : [
        '-i', OUTRO_PATH,
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
        '-vf', `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=${FPS}`,
        '-map', '0:v', '-map', '1:a',
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
  ideas: { quadrant: number; audioPath: string; caption: string }[];
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
    console.log(`[video] فكرة ${i + 1}/${ideas.length} (زوم على الربع ${ideas[i].quadrant})`);
    clips.push(await makeClip(gridImage, ideas[i].audioPath, path.join(workDir, `c_idea${i}.mp4`), workDir, `idea${i}`, {
      quadrant: ideas[i].quadrant, caption: ideas[i].caption,
    }));
  }

  console.log('[video] الختام');
  clips.push(await makeClip(gridImage, closingAudio, path.join(workDir, 'c_closing.mp4'), workDir, 'closing', {}));

  console.log('[video] التلاوة الثانية (مراجعة)');
  clips.push(await makeClip(gridImage, recitationPath, path.join(workDir, 'c_recite2.mp4'), workDir, 'recite2', {}));

  const outro = await normalizeOutro(workDir);
  if (outro) clips.push(outro);
  else console.warn('[video] تحذير: مفيش outro.mp4 — هيتمّ التجميع بدون أوترو.');

  const finalPath = path.join(workDir, 'final.mp4');
  console.log(`[video] دمج ${clips.length} مقطع -> final.mp4`);
  await concat(clips, finalPath, workDir);
  if (!fs.existsSync(finalPath) || fs.statSync(finalPath).size < 10000) {
    throw new Error('[video] الفيديو النهائي فاضي/فاسد.');
  }
  console.log(`[video] جاهز: ${finalPath}`);
  return finalPath;
}
