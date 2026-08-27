/**
 * QEEMA — Mushaf Page Service
 * بيجيب صورة صفحة المصحف (مصحف المدينة، 604 صفحة) اللي بتبدأ فيها السورة،
 * وبيقصّ الفراغ الأبيض حواليها عشان الآيات تملا الشاشة أثناء التلاوة الأولى.
 *
 * المصدر: GovarJabbar/Quran-PNG (صفحات مولّدة من مشروع quran.com الرسمي).
 * فشل التحميل/المعالجة مش بيوقف الحلقة — بنرجع null والفيديو يستخدم اللوحة بدلها.
 */
import * as fs from 'fs';
import * as path from 'path';
import { spawn } from 'child_process';
import { MUSHAF_BASE, VIDEO } from './config.ts';

const W = VIDEO.width, H = VIDEO.height;

/** صفحة بداية كل سورة في مصحف المدينة (الفاتحة + جزء عمّ). */
const SURAH_START_PAGE: Record<number, number> = {
  1: 1,
  78: 582, 79: 583, 80: 585, 81: 586, 82: 587, 83: 587, 84: 589, 85: 590,
  86: 591, 87: 591, 88: 592, 89: 593, 90: 594, 91: 595, 92: 595, 93: 596,
  94: 596, 95: 597, 96: 597, 97: 598, 98: 598, 99: 599, 100: 599, 101: 600,
  102: 600, 103: 601, 104: 601, 105: 601, 106: 602, 107: 602, 108: 602,
  109: 603, 110: 603, 111: 603, 112: 604, 113: 604, 114: 604,
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** يشغّل ffmpeg ويرجّع stderr (بنقرا منه ناتج فلتر bbox). */
function ffStderr(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const p = spawn('ffmpeg', args);
    let err = '';
    p.stderr.on('data', (d) => (err += d.toString()));
    p.on('close', (code) => (code === 0 ? resolve(err) : reject(new Error(err.slice(-400)))));
    p.on('error', reject);
  });
}

/** حدود المحتوى الفعلي (النص) في الصفحة — بفلتر bbox بعد عكس الألوان. */
async function contentBox(src: string): Promise<{ x: number; y: number; w: number; h: number } | null> {
  const out = await ffStderr(['-loop', '1', '-t', '0.1', '-i', src, '-vf', 'negate,bbox=min_val=40', '-f', 'null', '-']);
  const m = out.match(/x1:(\d+)\s+x2:(\d+)\s+y1:(\d+)\s+y2:(\d+)/);
  if (!m) return null;
  const x1 = +m[1], x2 = +m[2], y1 = +m[3], y2 = +m[4];
  if (x2 <= x1 || y2 <= y1) return null;
  return { x: x1, y: y1, w: x2 - x1 + 1, h: y2 - y1 + 1 };
}

/** أبعاد الصورة. */
async function imgSize(src: string): Promise<{ w: number; h: number }> {
  const out = await ffStderr(['-i', src, '-f', 'null', '-']).catch((e) => String(e.message || e));
  const m = out.match(/,\s(\d{2,5})x(\d{2,5})[\s,]/);
  if (!m) throw new Error('تعذّر قراءة أبعاد الصفحة');
  return { w: +m[1], h: +m[2] };
}

/** يقصّ الفراغ حوالين النص ويملا إطار 16:9 بخلفية بيضا. */
async function fitPage(src: string, dest: string): Promise<string> {
  const size = await imgSize(src);
  const box = await contentBox(src);
  let vf: string;
  if (box) {
    const mx = Math.round(box.w * 0.03), my = Math.round(box.h * 0.03); // هامش لطيف
    const x = Math.max(0, box.x - mx);
    const y = Math.max(0, box.y - my);
    const w = Math.min(size.w - x, box.w + 2 * mx);   // ما نتعداش حدود الصفحة
    const h = Math.min(size.h - y, box.h + 2 * my);
    vf = `crop=${w}:${h}:${x}:${y},`;
    console.log(`[mushaf] قص الفراغ: محتوى ${box.w}x${box.h} من صفحة ${size.w}x${size.h}`);
  } else {
    vf = '';
    console.warn('[mushaf] تعذّر تحديد حدود النص — هنعرض الصفحة كاملة.');
  }
  vf += `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1`;
  await ffStderr(['-y', '-i', src, '-vf', vf, '-frames:v', '1', dest]);
  return dest;
}

/** يحمّل صفحة المصحف للسورة ويجهّزها للعرض. يرجّع المسار أو null (بدون ما يرمي خطأ). */
export async function fetchMushafPage(surahNumber: number, workDir: string): Promise<string | null> {
  const page = SURAH_START_PAGE[surahNumber];
  if (!page) {
    console.warn(`[mushaf] مفيش صفحة معرّفة للسورة ${surahNumber} — هنستخدم اللوحة بدل المصحف.`);
    return null;
  }
  const url = `${MUSHAF_BASE}/${String(page).padStart(3, '0')}.png`;
  const raw = path.join(workDir, 'mushaf_raw.png');
  const dest = path.join(workDir, 'mushaf.png');

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length < 20000) throw new Error(`صورة صغيرة/فاسدة (${buf.length}B)`);
      fs.mkdirSync(path.dirname(raw), { recursive: true });
      fs.writeFileSync(raw, buf);
      await fitPage(raw, dest);
      if (!fs.existsSync(dest) || fs.statSync(dest).size < 5000) throw new Error('الناتج بعد المعالجة فاضي');
      console.log(`[mushaf] صفحة ${page} جاهزة للسورة ${surahNumber}`);
      return dest;
    } catch (err: any) {
      console.warn(`[mushaf] محاولة ${attempt}/3 فشلت: ${String(err?.message || err).slice(0, 140)}`);
      await sleep(1500 * attempt);
    }
  }
  console.warn('[mushaf] تعذّر تجهيز صفحة المصحف — هنكمّل باللوحة بدلها (مش خطأ قاتل).');
  return null;
}
