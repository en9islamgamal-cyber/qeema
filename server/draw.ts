/**
 * QEEMA — Real Hand-Drawing Engine  ("محرّك الرسم الحقيقي")
 * ---------------------------------------------------------------
 * بدل ما الصورة "تتكشف" بشكل هندسي، الكود بيحلّل الرسمة نفسها:
 *   1) يستخرج خطوط الاسكتش كبيانات خام (عبر ffmpeg — من غير أي مكتبة خارجية).
 *   2) يفصل الخطوط لـ"ضربات قلم" (مكوّنات متصلة) ويرتّبها زي إيد بترسم:
 *      من فوق لتحت، ومن اليمين للشمال، وكل ضربة تتّرسم من طرفها على طول الخط.
 *   3) يولّد قناع (mask) لكل فريم ويبعته لـ ffmpeg مباشرة عبر stdin،
 *      فالخطوط بتظهر نقطة تكبر تبقى خط، وخط ورا خط — رسم حقيقي.
 *
 * صفر مكتبات، صفر ملفات وسيطة، وخفيف على الذاكرة (فريم واحد في الذاكرة).
 */
import { spawn } from 'child_process';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

/** دقة تحليل الرسم (نصف الدقة = أسرع بكتير، والقناع بيتكبّر في الفلتر). */
export const MASK_W = 960;
export const MASK_H = 540;
const INK_THRESHOLD = 128; // أقل من كده = حبر

export interface StrokeOrder {
  order: Float32Array; // لكل بكسل: 0..1 = ترتيب رسمه، أو -1 لو مش حبر
  inkCount: number;
  strokes: number;
}

/** يستخرج خطوط الاسكتش كبايتات رمادية خام عبر ffmpeg. */
async function extractEdges(sketchPath: string): Promise<Buffer> {
  const { stdout } = await execFileAsync(
    'ffmpeg',
    [
      '-hide_banner', '-loglevel', 'error',
      '-i', sketchPath,
      '-vf', `scale=${MASK_W}:${MASK_H},edgedetect=low=0.1:high=0.3,negate,eq=saturation=0,format=gray`,
      '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1',
    ],
    { encoding: 'buffer', maxBuffer: 1024 * 1024 * 64 }
  );
  return stdout as unknown as Buffer;
}

/**
 * يحلّل الرسمة لضربات قلم مرتّبة.
 * بيرجّع null لو الرسمة فاضية/غريبة (وساعتها بنرجع للتأثير العادي).
 */
export async function computeStrokeOrder(sketchPath: string): Promise<StrokeOrder | null> {
  let g: Buffer;
  try {
    g = await extractEdges(sketchPath);
  } catch {
    return null;
  }
  const N = MASK_W * MASK_H;
  if (!g || g.length < N) return null;

  const isInk = new Uint8Array(N);
  let inkCount = 0;
  for (let i = 0; i < N; i++) {
    if (g[i] < INK_THRESHOLD) { isInk[i] = 1; inkCount++; }
  }
  // رسمة فاضية أو مليانة أوي = مش هينفع نرسمها ضربة ضربة
  if (inkCount < 200 || inkCount > N * 0.45) return null;

  // ١) فصل الضربات (مكوّنات متصلة 8-جوار)
  const comp = new Int32Array(N).fill(-1);
  const stack = new Int32Array(inkCount);
  const comps: { id: number; px: Int32Array; cx: number; cy: number }[] = [];
  const tmp = new Int32Array(inkCount);

  for (let s = 0; s < N; s++) {
    if (!isInk[s] || comp[s] !== -1) continue;
    const id = comps.length;
    let sp = 0, n = 0, sumX = 0, sumY = 0;
    stack[sp++] = s; comp[s] = id;
    while (sp > 0) {
      const p = stack[--sp];
      tmp[n++] = p;
      const y = (p / MASK_W) | 0, x = p - y * MASK_W;
      sumX += x; sumY += y;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= MASK_W || ny >= MASK_H) continue;
          const q = ny * MASK_W + nx;
          if (isInk[q] && comp[q] === -1) { comp[q] = id; stack[sp++] = q; }
        }
      }
    }
    comps.push({ id, px: tmp.slice(0, n), cx: sumX / n, cy: sumY / n });
  }

  // ٢) ترتيب الضربات: من فوق لتحت، ومن اليمين للشمال (إحساس عربي طبيعي)
  comps.sort((a, b) => (a.cy - b.cy) || (b.cx - a.cx));

  // ٣) داخل كل ضربة: BFS من أعلى نقطة = القلم بيمشي على الخط
  const order = new Float32Array(N).fill(-1);
  const seen = new Uint8Array(N);
  const queue = new Int32Array(inkCount);
  let k = 0;

  for (const c of comps) {
    let start = c.px[0], bestY = Infinity, bestX = -1;
    for (let i = 0; i < c.px.length; i++) {
      const p = c.px[i], y = (p / MASK_W) | 0, x = p - y * MASK_W;
      if (y < bestY || (y === bestY && x > bestX)) { bestY = y; bestX = x; start = p; }
    }
    let head = 0, tail = 0;
    queue[tail++] = start; seen[start] = 1;
    while (head < tail) {
      const p = queue[head++];
      order[p] = k++;
      const y = (p / MASK_W) | 0, x = p - y * MASK_W;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= MASK_W || ny >= MASK_H) continue;
          const r = ny * MASK_W + nx;
          if (isInk[r] && comp[r] === c.id && !seen[r]) { seen[r] = 1; queue[tail++] = r; }
        }
      }
    }
  }
  if (k < 2) return null;

  const denom = k - 1;
  for (let i = 0; i < N; i++) if (order[i] >= 0) order[i] = order[i] / denom;

  return { order, inkCount, strokes: comps.length };
}

/**
 * يشغّل ffmpeg ويغذّيه أقنعة الرسم فريم بفريم عبر stdin.
 * @param progressAt دالة: رقم الفريم -> نسبة الرسم (0..1)
 */
export function runWithDrawMask(
  args: string[],
  so: StrokeOrder,
  totalFrames: number,
  progressAt: (frame: number) => number
): Promise<void> {
  return new Promise((resolve, reject) => {
    const ff = spawn('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', ...args], {
      stdio: ['pipe', 'ignore', 'pipe'],
    });
    let errOut = '';
    ff.stderr.on('data', (d) => { errOut += d.toString(); });
    ff.on('error', reject);
    ff.on('close', (code) =>
      code === 0 ? resolve() : reject(new Error(`[draw] ffmpeg فشل (${code}): ${errOut.slice(-400)}`))
    );

    const N = MASK_W * MASK_H;
    const buf = Buffer.allocUnsafe(N);
    const order = so.order;
    let f = 0;

    const pump = () => {
      while (f < totalFrames) {
        const p = progressAt(f);
        for (let i = 0; i < N; i++) {
          const o = order[i];
          buf[i] = o < 0 ? 255 : (o <= p ? 255 : 0); // غير حبر = معتم | حبر مرسوم = ظاهر | لسه = شفاف
        }
        f++;
        if (!ff.stdin.write(buf)) { ff.stdin.once('drain', pump); return; }
      }
      ff.stdin.end();
    };
    ff.stdin.on('error', () => { /* الأنبوب اتقفل من ffmpeg — الخطأ بيتمسك في close */ });
    pump();
  });
}
