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
import { computeStrokeOrder, runWithDrawMask, MASK_W, MASK_H } from './draw.ts';
import { VIDEO, LOGO_PATH, OUTRO_PATH, ARABIC_FONT, ASSETS_DIR, INTRO_AUDIO_PATH, DRAW_REVEAL, REVEAL_END_BUFFER, REVEAL_PACE, REVEAL_SKETCH_TR, REVEAL_COLOR_TR, DRAW_REAL, PENCIL_VOLUME, PENCIL_IMG, PENCIL_SND } from './config.ts';

const execFileAsync = promisify(execFile);
const W = VIDEO.width, H = VIDEO.height, FPS = VIDEO.fps;
const INTRO_VIDEO_PATH = path.join(ASSETS_DIR, 'intro.mp4'); 
const THUMBNAIL_BG_PATH = path.join(ASSETS_DIR, 'thumbnail.png');

async function ff(args: string[]): Promise<void> {
  await execFileAsync('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', ...args], { maxBuffer: 1024 * 1024 * 64 });
}
export async function ffprobeDuration(file: string): Promise<number> {
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

export async function buildGrid(
  sketchPaths: string[],
  workDir: string,
  drawnCount: number = -1,
  outName: string = 'grid.png'
): Promise<string> {
  const n = sketchPaths.length;
  const k = drawnCount < 0 ? n : Math.min(drawnCount, n); // -1 = الكل مرسوم
  const layout = cellLayout(n);
  const inputs: string[] = ['-f', 'lavfi', '-i', `color=white:s=${W}x${H}`];
  for (let i = 0; i < k; i++) inputs.push('-i', sketchPaths[i]);

  const parts: string[] = [];
  for (let i = 0; i < k; i++) {
    parts.push(`[${i + 1}:v]scale=${layout[i].w}:${layout[i].h}:force_original_aspect_ratio=increase,crop=${layout[i].w}:${layout[i].h},setsar=1[s${i}]`);
  }
  let last = '[0:v]';
  if (k === 0) {
    parts.push(`[0:v]null[grid]`); // لوحة فاضية بالكامل
  } else {
    for (let i = 0; i < k; i++) {
      const out = i === k - 1 ? '[grid]' : `[o${i}]`;
      parts.push(`${last}[s${i}]overlay=${layout[i].x}:${layout[i].y}${out}`);
      last = `[o${i}]`;
    }
  }

  const grid = path.join(workDir, outName);
  await ff([...inputs, '-filter_complex', parts.join(';'), '-map', '[grid]', '-frames:v', '1', grid]);
  return grid;
}

/** مستطيل خلية أو الشاشة كاملة */
type ZoomRect = { x: number; y: number; w: number; h: number } | null; // null = الشاشة كاملة

/**
 * مقطع زوم صامت على اللوحة (بدون سرد) — بـ zoompan (نفس نمط مسار الـ focus المجرّب):
 *  - from خلية + to خلية: زوم أوت للوحة كاملة (يشوف المرسوم والفاضي) ثم زوم إن على الخلية الجاية.
 *  - from خلية + to null: زوم أوت فقط. from null + to خلية: زوم إن فقط.
 * الزوم الإن على خلية فاضية بينتهي بشاشة بيضا = يكمل طبيعي مع بداية "صفحة" الفكرة الجاية.
 */
export async function makeZoomClip(
  gridPath: string,
  outPath: string,
  secs: number,
  fromRect: ZoomRect,
  toRect: ZoomRect,
  tag: string
): Promise<string> {
  const frames = Math.max(2, Math.round(secs * FPS));
  const zOf = (r: ZoomRect) => (r ? Math.min(W / r.w, H / r.h) : 1); // زوم الخلية (2 في شبكة 2×2)
  const cOf = (r: ZoomRect) => (r ? { cx: r.x + r.w / 2, cy: r.y + r.h / 2 } : { cx: W / 2, cy: H / 2 });
  const zA = zOf(fromRect), zB = zOf(toRect);
  const A = cOf(fromRect), B = cOf(toRect);
  const both = !!fromRect && !!toRect;

  const f1 = both ? Math.round(frames * 0.42) : frames;
  const fh = both ? Math.round(frames * 0.16) : 0;
  const f3 = Math.max(1, frames - f1 - fh);

  // smoothstep على رقم الفريم (on)
  const sm = (r: string) => `(${r})*(${r})*(3-2*(${r}))`;
  const phase = (v0: number, v1: number, onExpr: string, len: number) =>
    `(${v0}+(${v1 - v0})*${sm(`clip(${onExpr}/${len}\\,0\\,1)`)})`;

  const pw = (v0: number, vMid: number, v1: number) =>
    both
      ? `if(lt(on\\,${f1 + fh})\\,${phase(v0, vMid, 'on', f1)}\\,${phase(vMid, v1, `(on-${f1 + fh})`, f3)})`
      : phase(v0, v1, 'on', f1);

  const zExpr = pw(zA, 1, zB);
  const cxExpr = pw(A.cx, W / 2, B.cx);
  const cyExpr = pw(A.cy, H / 2, B.cy);
  const x = `max(0\\,min((${cxExpr})-(iw/zoom/2)\\,iw-iw/zoom))`;
  const y = `max(0\\,min((${cyExpr})-(ih/zoom/2)\\,ih-ih/zoom))`;

  const chain =
    `[0:v]scale=${W}:${H},setsar=1,zoompan=z='${zExpr}':x='${x}':y='${y}':d=1:s=${W}x${H}:fps=${FPS},format=yuv420p[v0];` +
    `[1:v]scale=300:-1[lg];[v0][lg]overlay=W-w-40:H-h-40[v]`;

  await ff([
    '-framerate', String(FPS), '-loop', '1', '-t', String(secs), '-i', gridPath,
    '-loop', '1', '-t', String(secs), '-i', LOGO_PATH,
    '-f', 'lavfi', '-t', String(secs), '-i', 'anullsrc=r=44100:cl=stereo',
    '-filter_complex', chain,
    '-map', '[v]', '-map', '2:a', '-t', String(secs),
    '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-r', String(FPS),
    '-c:a', 'aac', '-ar', '44100', '-ac', '2',
    outPath,
  ]);
  return outPath;
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
interface ClipOpts { focus?: Rect; caption?: string; reveal?: boolean; revealStart?: number; }

async function makeClip(visual: string, audio: string, outPath: string, workDir: string, tag: string, opts: ClipOpts = {}): Promise<string> {
  const dur = await ffprobeDuration(audio);
  if (dur <= 0) throw new Error(`[video] صوت بلا مدة: ${audio}`);
  const frames = Math.ceil(dur * FPS);

  // هل نطبّق تأثير الرسم؟ (بس لو مفعّل، والمقطع طويل كفاية)
  const wantReveal = !!opts.reveal && DRAW_REVEAL;
  const off = Math.max(0, opts.revealStart || 0);          // نهاية التلاوة المقطّعة = بداية الشرح
  // === توقيتات المراحل التلاتة ===
  // مرحلة 1: صفحة بيضا لحظة قصيرة، ثم "رسم" خطوط القلم الرصاص أثناء التلاوة (بدون صوت قلم احترامًا للقرآن).
  // لو التلاوة قصيرة، الاسكتش يكمّل رسمه شوية في بداية الشرح.
  // مرحلة 2: التلوين التدريجي مع الشرح (بصوت قلم واضح) ويكتمل قبل نهاية المقطع بـ REVEAL_END_BUFFER.
  const o1 = 0.3;
  const colorEnd = dur - REVEAL_END_BUFFER;
  let R1: number;
  if (off > 1) {
    const spill = Math.min(3, Math.max(0, (dur - off) * 0.25)); // امتداد الاسكتش داخل بداية الشرح
    R1 = (off - o1) + spill;
  } else {
    R1 = Math.min(4, Math.max(1.5, dur * 0.25)); // مفيش تلاوة (fallback) — اسكتش قصير
  }
  // ضمان مساحة كافية للتلوين (1.5 ثانية على الأقل)
  R1 = Math.min(R1, Math.max(1.0, colorEnd - 1.5 - o1));
  const o2 = o1 + R1;                                        // بداية التلوين
  // التلوين بياخد نسبة من الوقت المتاح (أسرع)، وبعدها الصورة تفضل مكتملة شوية
  const avail = Math.max(1.0, colorEnd - o2);
  const R2 = Math.max(1.0, avail * REVEAL_PACE);
  const doReveal = wantReveal && dur >= 6 && R1 >= 1.0 && R2 >= 1.0 && o2 + R2 <= dur;

  const inputs: string[] = [
    '-framerate', String(FPS), '-loop', '1', '-t', String(dur), '-i', visual, // 0: الصورة
    '-i', audio,                                                                // 1: الصوت
    '-loop', '1', '-t', String(dur), '-i', LOGO_PATH,                          // 2: اللوجو
  ];
  let idx = 3;
  let penImgIdx = -1, penSndIdx = -1, colIdx = -1, maskIdx = -1;
  const hasPenImg = doReveal && fs.existsSync(PENCIL_IMG);
  const hasPenSnd = doReveal && fs.existsSync(PENCIL_SND);
  if (hasPenImg) { inputs.push('-loop', '1', '-t', String(dur), '-i', PENCIL_IMG); penImgIdx = idx++; }
  if (hasPenSnd) { inputs.push('-stream_loop', '-1', '-i', PENCIL_SND); penSndIdx = idx++; } // -stream_loop: صوت القلم يتكرّر ويملى طول الرسم
  if (doReveal) {
    // نسخة تانية مستقلة من الصورة لفرع الألوان — إصلاح جذري: split كان بيسرّب eq=saturation=0
    // من فرع الاسكتش لفرع الألوان (مشاركة فريمات في ffmpeg) فالفيديو كله كان بيطلع أبيض وأسود.
    inputs.push('-framerate', String(FPS), '-loop', '1', '-t', String(dur), '-i', visual);
    colIdx = idx++;
  }

  const chain: string[] = [];

  // ===== محرّك الرسم الحقيقي: تحليل الرسمة لضربات قلم =====
  let strokeOrder: Awaited<ReturnType<typeof computeStrokeOrder>> = null;
  if (doReveal && DRAW_REAL) {
    strokeOrder = await computeStrokeOrder(visual);
    if (strokeOrder) {
      console.log(`[draw] ${tag}: ${strokeOrder.strokes} ضربة قلم (${strokeOrder.inkCount} نقطة حبر) — رسم حقيقي.`);
      inputs.push('-f', 'rawvideo', '-pix_fmt', 'gray', '-s', `${MASK_W}x${MASK_H}`, '-r', String(FPS), '-i', 'pipe:0');
      maskIdx = idx++;
    } else {
      console.warn(`[draw] ${tag}: تعذّر تحليل الرسمة لضربات — هنستخدم التأثير العادي.`);
    }
  }

  // ===== بناء الفيديو =====
  if (doReveal && strokeOrder) {
    // رسم حقيقي: الخطوط بتترسم ضربة ورا ضربة (قناع من الكود) ثم التلوين
    const fit = `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1,fps=${FPS}`;
    const skF = `edgedetect=low=0.1:high=0.3,negate,eq=saturation=0`;
    chain.push(`[0:v]${fit},${skF},format=rgba[skv]`);
    chain.push(`[${maskIdx}:v]scale=${W}:${H}:flags=bicubic,format=gray[mk]`);
    chain.push(`[skv][mk]alphamerge[ska]`);
    chain.push(`color=c=white:s=${W}x${H}:r=${FPS}:d=${dur.toFixed(2)}[bg]`);
    chain.push(`[bg][ska]overlay=shortest=1,format=yuv420p,trim=0:${dur.toFixed(2)},setpts=PTS-STARTPTS[drawn]`);
    chain.push(`[${colIdx}:v]${fit},format=yuv420p,trim=0:${(dur - o2 + 0.2).toFixed(2)},setpts=PTS-STARTPTS[colv]`);
    chain.push(`[drawn][colv]xfade=transition=${REVEAL_COLOR_TR}:duration=${R2.toFixed(2)}:offset=${o2.toFixed(2)},trim=0:${dur.toFixed(2)},setpts=PTS-STARTPTS[rev]`);
    chain.push(`[rev]fade=t=in:st=0:d=0.3[v0]`);
  } else if (doReveal) {
    // صفحة بيضا -> خطوط قلم رصاص تظهر كنقط بتكبر وتتجمّع (dissolve = إحساس الرسم اليدوي)
    // -> تلوين تدريجي بحافة حادة من الركن (wipetl). الحواف الحادة = الخطوط تظهر زي القلم،
    //    مش تفتيح تدريجي للصورة (ده كان سبب إحساس "بهتة وبعدين توضح").
    const skF = `edgedetect=low=0.1:high=0.3,negate,eq=saturation=0,format=yuv420p`;
    const fit = `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1,fps=${FPS}`;
    // قص المصادر لأقل مدة مطلوبة (توفير ذاكرة ووقت رندر)
    chain.push(`color=c=white:s=${W}x${H}:r=${FPS}:d=${(o1 + R1 + 0.2).toFixed(2)}[wh]`);
    chain.push(`[0:v]${fit},${skF},trim=0:${(dur - o1 + 0.2).toFixed(2)},setpts=PTS-STARTPTS[skv]`);
    chain.push(`[${colIdx}:v]${fit},format=yuv420p,trim=0:${(dur - o2 + 0.2).toFixed(2)},setpts=PTS-STARTPTS[colv]`);
    chain.push(`[wh][skv]xfade=transition=${REVEAL_SKETCH_TR}:duration=${R1.toFixed(2)}:offset=${o1.toFixed(2)},trim=0:${dur.toFixed(2)},setpts=PTS-STARTPTS[s1]`);
    chain.push(`[s1][colv]xfade=transition=${REVEAL_COLOR_TR}:duration=${R2.toFixed(2)}:offset=${o2.toFixed(2)},trim=0:${dur.toFixed(2)},setpts=PTS-STARTPTS[rev]`);
    chain.push(`[rev]fade=t=in:st=0:d=0.3[v0]`);
  } else if (opts.focus) {
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

  // قلم بيتحرك على خط الكشف أثناء الرسم
  let vCur = '[v0]';
  if (hasPenImg) {
    chain.push(`[${penImgIdx}:v]scale=150:-1[pen]`);
    chain.push(`${vCur}[pen]overlay=x=${W}*0.72:y='((t-${o2.toFixed(2)})/${R2.toFixed(2)})*${H} - h*0.8':enable='between(t,${o2.toFixed(2)},${(o2 + R2).toFixed(2)})'[vpen]`);
    vCur = '[vpen]';
  }

  // اللوجو
  chain.push(`[2:v]scale=300:-1[lg]`);
  chain.push(`${vCur}[lg]overlay=W-w-40:H-h-40[v1]`);

  let lastV = '[v1]';
  if (opts.caption && opts.caption.trim()) {
    const assPath = writeAss(opts.caption.trim(), workDir, tag);
    chain.push(`[v1]subtitles='${escFilter(assPath)}'[v2]`);
    lastV = '[v2]';
  }

  // ===== بناء الصوت =====
  let audioMap = '1:a';
  if (hasPenSnd) {
    // صوت القلم أثناء التلوين فقط (مش فوق التلاوة) — واضح، مع دخول/خروج ناعم
    const delayMs = Math.round(o2 * 1000);
    const fadeSt = Math.max(0.5, R2 - 1).toFixed(2);
    chain.push(`[${penSndIdx}:a]atrim=0:${R2.toFixed(2)},asetpts=PTS-STARTPTS,volume=${PENCIL_VOLUME},afade=t=in:st=0:d=0.3,afade=t=out:st=${fadeSt}:d=1,adelay=${delayMs}|${delayMs},apad[pa]`);
    chain.push(`[1:a]apad[ma]`);
    chain.push(`[ma][pa]amix=inputs=2:duration=first:normalize=0[aout]`);
    audioMap = '[aout]';
  }

  const ffArgs = [
    ...inputs,
    '-filter_complex', chain.join(';'),
    '-map', lastV, '-map', audioMap, '-t', String(dur),
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', String(FPS),
    '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '192k',
    '-shortest', outPath,
  ];

  if (strokeOrder) {
    // الرسم الحقيقي: بنغذّي ffmpeg قناع كل فريم من الكود
    const totalFrames = Math.ceil(dur * FPS) + 2;
    const f1 = o1 * FPS, f2 = (o1 + R1) * FPS;
    await runWithDrawMask(ffArgs, strokeOrder, totalFrames, (f) => {
      if (f <= f1) return 0;
      if (f >= f2) return 1;
      return (f - f1) / Math.max(1, f2 - f1);
    });
  } else {
    await ff(ffArgs);
  }
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

/**
 * يضغط/يحوّل أي صورة ثمبنايل لـ JPEG 1280×720 أقل من 2MB (حد يوتيوب).
 * بيشتغل لأي مصدر (نص مرسوم / AI / صورة من الاسيتس).
 */
export async function compressThumbnail(srcPath: string, workDir: string): Promise<string> {
  const out = path.join(workDir, 'thumb_final.jpg');
  await ff([
    '-i', srcPath,
    '-vf', `scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1`,
    '-frames:v', '1', '-q:v', '3', out,
  ]);
  // ضمان إضافي: لو لسه فوق ~2MB (نادر) نعيد بجودة أقل
  try {
    const sz = fs.statSync(out).size;
    if (sz > 2_000_000) {
      await ff(['-i', srcPath, '-vf', `scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1`, '-frames:v', '1', '-q:v', '7', out]);
    }
  } catch { /* تجاهل */ }
  return out;
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
  ideas: { focus: Rect; audioPath: string; caption: string; sketch?: string; revealStart?: number }[];
  introCaption: string;
  mushafPath?: string | null;   // صفحة المصحف للتلاوة الأولى (اختياري)
  gridStates?: string[];        // اللوحة الحية: [0]=فاضية … [n]=كاملة
  introVisual?: string;         // صورة المقدمة المتغيّرة (بدل اللوحة الكاملة عشان مانحرقش المفاجأة)
}

export async function assembleEpisode(input: AssemblyInput): Promise<string> {
  const { workDir, gridImage, recitationPath, introAudio, closingAudio, ideas } = input;
  const gs = input.gridStates && input.gridStates.length === ideas.length + 1 ? input.gridStates : null;
  const layout = cellLayout(ideas.length);
  const clips: string[] = [];
  const labels: string[] = [];
  const push = (p: string, label: string) => { clips.push(p); labels.push(label); };

  const introSeg = await normalizeIntro(workDir);
  if (introSeg) {
    console.log('[video] مقطع الانترو الثابت');
    push(introSeg, 'intro_fixed');
  } else {
    console.warn('[video] تحذير: مفيش انترو ثابت (assets/intro.mp4 أو assets/intro.mp3) — شغّل make_intro.ts.');
  }

  console.log('[video] مقطع المقدمة (المتغيّر)');
  push(await makeClip(input.introVisual || gridImage, introAudio, path.join(workDir, 'c_intro.mp4'), workDir, 'intro', { caption: input.introCaption }), 'intro');

  console.log('[video] التلاوة الأولى' + (input.mushafPath ? ' (صفحة المصحف)' : ''));
  push(await makeClip(input.mushafPath || gridImage, recitationPath, path.join(workDir, 'c_recite1.mp4'), workDir, 'recite1', {}), 'recite1');

  if (input.bridgeAudio) {
    console.log('[video] الفاصل (تمهيد للتلاوة المقطّعة)');
    // اللوحة الفاضية = غموض: الطفل شايف صفحات بيضا مستنية تترسم
    push(await makeClip(gs ? gs[0] : gridImage, input.bridgeAudio, path.join(workDir, 'c_bridge.mp4'), workDir, 'bridge', {}), 'bridge');
  }

  // زوم إن من اللوحة الفاضية على مكان الفكرة الأولى (نهايته خلية بيضا = بداية صفحة الفكرة)
  if (gs) {
    console.log('[video] زوم إن على مكان الفكرة الأولى');
    push(await makeZoomClip(gs[0], path.join(workDir, 'z_in0.mp4'), 1.8, null, layout[0], 'z_in0'), 'z_in0');
  }

  for (let i = 0; i < ideas.length; i++) {
    console.log(`[video] فكرة ${i + 1}/${ideas.length}`);
    const it = ideas[i];
    if (it.sketch) {
      // صفحة بيضا -> اسكتش يترسم مع التلاوة -> تلوين مع الشرح (بصوت القلم)
      push(await makeClip(it.sketch, it.audioPath, path.join(workDir, `c_idea${i}.mp4`), workDir, `idea${i}`, {
        caption: it.caption, reveal: true, revealStart: it.revealStart || 0,
      }), `idea${i}`);
    } else {
      push(await makeClip(gridImage, it.audioPath, path.join(workDir, `c_idea${i}.mp4`), workDir, `idea${i}`, {
        focus: it.focus, caption: it.caption,
      }), `idea${i}`);
    }
    // بعد كل فكرة: زوم أوت للوحة (الأفكار اللي خلصت مرسومة والباقي فاضي) ثم زوم إن على الجاية
    if (gs) {
      if (i < ideas.length - 1) {
        console.log(`[video] لوحة حية: ${i + 1} مرسومة → زوم على الفكرة ${i + 2}`);
        push(await makeZoomClip(gs[i + 1], path.join(workDir, `z_t${i}.mp4`), 3.2, layout[i], layout[i + 1], `z_t${i}`), `z_t${i}`);
      } else {
        console.log('[video] زوم أوت أخير: اللوحة كاملة مرسومة');
        push(await makeZoomClip(gs[ideas.length], path.join(workDir, `z_out.mp4`), 2.2, layout[i], null, 'z_out'), 'z_out');
      }
    }
  }

  console.log('[video] الختام');
  push(await makeClip(gridImage, closingAudio, path.join(workDir, 'c_closing.mp4'), workDir, 'closing', {}), 'closing');

  const outro = await normalizeOutro(workDir);
  if (outro) push(outro, 'outro');
  else console.warn('[video] تحذير: مفيش outro.mp4 — هيتمّ التجميع بدون أوترو.');

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
