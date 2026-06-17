/**
 * QEEMA — Video Assembly (FFmpeg حقيقي)
 * يبني الخط الزمني:
 * مقدمة (صورة كاملة) ← تلاوة 1 (كاملة) ← [زوم على كل فكرة + شرحها] ← ختام (كاملة)
 * ← تلاوة 2 (كاملة) ← الأوترو.
 * + واترمارك اللوجو + تعليقات عربية محروقة (ASS/libass).
 * يتطلّب ffmpeg/ffprobe وخط عربي (Noto Naskh Arabic) مركّبين.
 */
import * as fs from 'fs';
import * as path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { VIDEO, LOGO_PATH, OUTRO_PATH, ARABIC_FONT, ASSETS_DIR, INTRO_AUDIO_PATH } from './config.ts';

const execFileAsync = promisify(execFile);
const W = VIDEO.width, H = VIDEO.height, FPS = VIDEO.fps;
const INTRO_VIDEO_PATH = path.join(ASSETS_DIR, 'intro.mp4'); 
const THUMBNAIL_BG_PATH = path.join(ASSETS_DIR, 'thumbnail.png');

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

export function cellLayout(n: number): { x: number; y: number; w: number; h: number }[] {
  const cw = Math.floor(W / 2), ch = Math.floor(H / 2);
  if (n <= 3) {
    return [
      { x: 0, y: 0, w: cw, h: ch },
      { x: cw, y: 0, w: cw, h: ch },
      { x: Math.floor(W / 4), y: ch, w: cw, h: ch },
    ].slice(0, n);
  }
  return [
    { x: 0, y: 0, w: cw, h: ch },
    { x: cw, y: 0, w: cw, h: ch },
    { x: 0, y: ch, w: cw, h: ch },
    { x: cw, y: ch, w: cw, h: ch },
  ];
}

export async function buildGrid(sketchPaths: string[], workDir: string): Promise<string> {
  const n = sketchPaths.length;
  const layout = cellLayout(n);
  const inputs: string[] = ['-f', 'lavfi', '-i', `color=white:s=${W}x${H}`];
  sketchPaths.forEach((p) => inputs.push('-i', p));

  const parts: string[] = [];
  sketchPaths.forEach((_, i) => {
    parts.push(`[${i + 1}:v]scale=${layout[i].w}:${layout[i].h}:force_original_aspect_ratio=increase,crop=${layout[i].w}:${layout[i].h},setsar=1[s${i}]`);
  });
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
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,9:59:59.00,Q,,0,0,0,,${safe}
`;
  const p = path.join(workDir, `cap_${tag}.ass`);
  fs.writeFileSync(p, ass, 'utf-8');
  return p;
}

const escFilter = (p: string) => p.replace(/\\/g, '/').replace(/:/g, '\\:').replace(/'/g, "\\'");

interface Rect { x: number; y: number; w: number; h: number; }
interface ClipOpts { focus?: Rect; caption?: string; }

async function makeClip(visual: string, audio: string, outPath: string, workDir: string, tag: string, opts: ClipOpts = {}): Promise<string> {
  const dur = await ffprobeDuration(audio);
  if (dur <= 0) throw new Error(`[video] صوت بلا مدة: ${audio}`);
  const frames = Math.ceil(dur * FPS);

  const chain: string[] = [];
  if (opts.focus) {
    const cx = opts.focus.x + opts.focus.w / 2;
    const cy = opts.focus.y + opts.focus.h / 2;
    let inF = Math.round(1.2 * FPS);
    let outF = Math.round(1.0 * FPS);
    if (frames < inF + outF + FPS) { inF = Math.round(0.8 * FPS); outF = Math.round(0.6 * FPS); }
    const outStart = Math.max(inF + 1, frames - outF);
    const z = `if(lt(on,${inF}),1+on/${inF},if(lt(on,${outStart}),2,max(1,2-(on-${outStart})/${outF})))`;
    const x = `max(0\\,min(${cx}-(iw/zoom/2)\\,iw-iw/zoom))`;
    const y = `max(0\\,min(${cy}-(ih/zoom/2)\\,ih-ih/zoom))`;
    chain.push(`[0:v]scale=${W}:${H},setsar=1,zoompan=z='${z}':x='${x}':y='${y}':d=1:s=${W}x${H}:fps=${FPS}[v0]`);
  } else {
    chain.push(`[0:v]scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1,fps=${FPS}[v0]`);
  }
  
  chain.push(`[2:v]scale=300:-1[lg]`);
  chain.push(`[v0][lg]overlay=W-w-40:H-h-40[v1]`);
  
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
    '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '192k',
    '-shortest', outPath,
  ]);
  return outPath;
}

async function normalizeIntro(workDir: string): Promise<string | null> {
  const hasNar = fs.existsSync(INTRO_AUDIO_PATH);

  if (fs.existsSync(INTRO_VIDEO_PATH)) {
    const out = path.join(workDir, 'intro_norm.mp4');
    const vDur = await ffprobeDuration(INTRO_VIDEO_PATH);
    if (vDur <= 0) { console.warn('[video] تحذير: intro.mp4 بلا مدة صالحة — تخطّينا الانترو.'); return null; }
    const vHasAudio = await hasAudio(INTRO_VIDEO_PATH);

    // مستوى صوت موسيقى الانترو (اللي جوّه intro.mp4) — متحكَّم فيه من البيئة (كان 0.28 = شبه مكتوم).
    const bgVol = parseFloat(process.env.INTRO_BG_VOLUME || '0.4');
    // لو التعليق أطول من فيديو الانترو نمدّ المقطع (نثبّت آخر فريم) عشان الصوت ما يتقصّش.
    const narDur = hasNar ? await ffprobeDuration(INTRO_AUDIO_PATH) : 0;
    const segDur = Math.max(vDur, narDur);
    const vpad = Math.max(0, segDur - vDur);
    const tpadF = vpad > 0.05 ? `,tpad=stop_mode=clone:stop_duration=${vpad.toFixed(3)}` : '';
    console.log(`[video] انترو: فيديو=${vDur.toFixed(1)}s تعليق=${narDur.toFixed(1)}s -> مقطع=${segDur.toFixed(1)}s`);

    const inputs: string[] = ['-i', INTRO_VIDEO_PATH];
    let idx = 1;
    let narIdx = -1;
    if (hasNar) { inputs.push('-i', INTRO_AUDIO_PATH); narIdx = idx++; }
    
    // إضافة اللوجو بشكل دائم على الانترو ليكون مطابق لباقي الفيديو
    inputs.push('-loop', '1', '-i', LOGO_PATH);
    const logoIdx = idx++;

    let vchain =
      `[0:v]split=2[bg][fg];` +
      `[bg]scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},boxblur=22:4,eq=brightness=-0.05,setsar=1[bgb];` +
      `[fg]scale=${W}:${H}:force_original_aspect_ratio=decrease,setsar=1[fgs];` +
      `[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base];` +
      `[${logoIdx}:v]scale=300:-1,format=rgba[lg];` +
      `[base][lg]overlay=W-w-40:H-h-40,fps=${FPS}${tpadF}[v]`;

    let achain = '';
    let amap: string;
    const extra: string[] = [];
    if (vHasAudio && hasNar) {
      achain = `;[0:a]volume=${bgVol},apad[m];[${narIdx}:a]volume=1.0,apad[n];[m][n]amix=inputs=2:duration=longest:normalize=0[a]`;
      amap = '[a]';
    } else if (vHasAudio && !hasNar) {
      amap = '0:a';
    } else if (!vHasAudio && hasNar) {
      achain = `;[${narIdx}:a]apad[a]`;
      amap = '[a]';
    } else {
      extra.push('-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100');
      amap = `${idx}:a`;
    }

    await ff([
      ...inputs, ...extra,
      '-filter_complex', vchain + achain,
      '-map', '[v]', '-map', amap, '-t', segDur.toFixed(3),
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
      '-c:a', 'aac', '-ar', '44100', '-b:a', '192k', out,
    ]);
    return out;
  }

  if (fs.existsSync(INTRO_AUDIO_PATH)) {
    const dur = await ffprobeDuration(INTRO_AUDIO_PATH);
    if (dur <= 0) return null;
    const out = path.join(workDir, 'intro_norm.mp4');
    const fadeOut = Math.max(0, dur - 0.6).toFixed(2);
    await ff([
      '-f', 'lavfi', '-t', String(dur),
      '-i', `gradients=s=${W}x${H}:c0=0x0f2a4a:c1=0x2f7fb5:x0=0:y0=0:x1=${W}:y1=${H}`,
      '-i', INTRO_AUDIO_PATH,
      '-loop', '1', '-t', String(dur), '-i', LOGO_PATH,
      '-filter_complex',
        `[2:v]scale=520:-1[lg];` +
        `[0:v]format=yuv420p,fps=${FPS}[bg];` +
        `[bg][lg]overlay=(W-w)/2:(H-h)/2-40,fade=t=in:st=0:d=0.5,fade=t=out:st=${fadeOut}:d=0.6[v]`,
      '-map', '[v]', '-map', '1:a',
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
      '-c:a', 'aac', '-ar', '44100', '-b:a', '192k', '-shortest', out,
    ]);
    return out;
  }

  return null;
}

async function normalizeOutro(workDir: string): Promise<string | null> {
  if (!fs.existsSync(OUTRO_PATH)) return null;
  const out = path.join(workDir, 'outro_norm.mp4');
  const audio = await hasAudio(OUTRO_PATH);

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
  // ⚠️ الـ concat demuxer كان بيسقّط الصوت بعد أول مقطع (تايمستامب غير متّسق + اختلاف قنوات).
  // الحل: concat filter — بنطبّع فيديو وصوت كل مقطع (ستيريو 44100) وبنعيد بناء تايملاين نظيف.
  const n = clips.length;
  const inputs: string[] = [];
  for (const c of clips) inputs.push('-i', c);

  let pre = '';
  let pads = '';
  for (let i = 0; i < n; i++) {
    pre +=
      `[${i}:v]scale=${W}:${H}:force_original_aspect_ratio=decrease,` +
      `pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=${FPS},format=yuv420p[v${i}];` +
      `[${i}:a]aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo[a${i}];`;
    pads += `[v${i}][a${i}]`;
  }
  const filter = `${pre}${pads}concat=n=${n}:v=1:a=1[v][a]`;

  await ff([
    ...inputs,
    '-filter_complex', filter,
    '-map', '[v]', '-map', '[a]',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
    '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '192k',
    outPath,
  ]);
}

export async function renderThumbnailText(lines: string[], workDir: string): Promise<string | null> {
  if (!fs.existsSync(THUMBNAIL_BG_PATH)) return null;
  const out = path.join(workDir, 'thumbnail.png');

  const clean = lines.map((s) => s.replace(/[{}\n]/g, ' ').trim()).filter(Boolean);
  if (clean.length === 0) return null;

  const titleSize = parseInt(process.env.THUMB_TITLE_SIZE || '104', 10);
  const subSize = Math.round(titleSize * 0.6);
  const opacity = process.env.THUMB_BAND_OPACITY || '0.70';

  const bandTop = Math.round(H * 0.74);
  const bandH = H - bandTop;
  const hasSub = clean.length >= 2;
  const blockH = hasSub ? titleSize * 1.25 + subSize * 1.3 : titleSize * 1.25;
  const marginV = Math.max(20, Math.round((bandH - blockH) / 2));

  const sub = hasSub ? `\\N{\\fs${subSize}\\c&H00FFFFFF&}${clean[1]}` : '';
  const dialogueText = `${clean[0]}${sub}`;

  const ass = `[Script Info]
ScriptType: v4.00+
PlayResX: ${W}
PlayResY: ${H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: T,${ARABIC_FONT},${titleSize},&H0066D9FF,&H003C1E0F,&H00000000,1,5,3,2,90,90,${marginV}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,${dialogueText}
`;
  const assPath = path.join(workDir, 'thumb.ass');
  fs.writeFileSync(assPath, ass, 'utf-8');

  await ff([
    '-i', THUMBNAIL_BG_PATH,
    '-filter_complex',
      `[0:v]scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H},setsar=1[bg];` +
      `[bg]drawbox=x=0:y=${bandTop}:w=${W}:h=${bandH}:color=0x0F1E3C@${opacity}:t=fill,` +
      `drawbox=x=0:y=${bandTop}:w=${W}:h=7:color=0xF5C85A:t=fill[bb];` +
      `[bb]subtitles='${escFilter(assPath)}'[v]`,
    '-map', '[v]', '-frames:v', '1', out,
  ]);

  if (!fs.existsSync(out)) throw new Error('[video] فشل إنتاج الثمبنايل.');
  return out;
}

export interface AssemblyInput {
  workDir: string;
  gridImage: string;
  recitationPath: string;
  introAudio: string;
  closingAudio: string;
  bridgeAudio?: string;
  ideas: { focus: Rect; audioPath: string; caption: string }[];
  introCaption: string;
}

export async function assembleEpisode(input: AssemblyInput): Promise<string> {
  const { workDir, gridImage, recitationPath, introAudio, closingAudio, ideas } = input;
  const clips: string[] = [];

  const introSeg = await normalizeIntro(workDir);
  if (introSeg) {
    console.log('[video] مقطع الانترو الثابت');
    clips.push(introSeg);
  } else {
    console.warn('[video] تحذير: مفيش انترو ثابت (assets/intro.mp4 أو assets/intro.mp3) — شغّل make_intro.ts.');
  }

  console.log('[video] مقطع المقدمة (المتغيّر)');
  clips.push(await makeClip(gridImage, introAudio, path.join(workDir, 'c_intro.mp4'), workDir, 'intro', { caption: input.introCaption }));

  console.log('[video] التلاوة الأولى');
  clips.push(await makeClip(gridImage, recitationPath, path.join(workDir, 'c_recite1.mp4'), workDir, 'recite1', {}));

  if (input.bridgeAudio) {
    console.log('[video] الفاصل (تمهيد للتلاوة المقطّعة)');
    clips.push(await makeClip(gridImage, input.bridgeAudio, path.join(workDir, 'c_bridge.mp4'), workDir, 'bridge', {}));
  }

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

  const labels = [...(introSeg ? ['intro_fixed'] : []), 'intro', 'recite1', ...(input.bridgeAudio ? ['bridge'] : []), ...ideas.map((_, i) => `idea${i}`), 'closing', ...(outro ? ['outro'] : [])];
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
