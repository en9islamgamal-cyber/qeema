/**
 * QEEMA — Mushaf Page Service
 * بيجيب صورة صفحة المصحف (مصحف المدينة، 604 صفحة) اللي بتبدأ فيها السورة،
 * لعرضها أثناء التلاوة الأولى.
 *
 * المصدر: GovarJabbar/Quran-PNG على GitHub (صفحات مولّدة من مشروع quran.com الرسمي).
 * الرابط: https://raw.githubusercontent.com/GovarJabbar/Quran-PNG/master/{page}.png
 *
 * فشل التحميل مش بيوقف الحلقة — بنرجع null والفيديو يستخدم اللوحة بدلها.
 */
import * as fs from 'fs';
import * as path from 'path';
import { MUSHAF_BASE } from './config.ts';

/** صفحة بداية كل سورة في مصحف المدينة (المنهج الحالي: الفاتحة + جزء عمّ). */
const SURAH_START_PAGE: Record<number, number> = {
  1: 1,
  78: 582, 79: 583, 80: 585, 81: 586, 82: 587, 83: 587, 84: 589, 85: 590,
  86: 591, 87: 591, 88: 592, 89: 593, 90: 594, 91: 595, 92: 595, 93: 596,
  94: 596, 95: 597, 96: 597, 97: 598, 98: 598, 99: 599, 100: 599, 101: 600,
  102: 600, 103: 601, 104: 601, 105: 601, 106: 602, 107: 602, 108: 602,
  109: 603, 110: 603, 111: 603, 112: 604, 113: 604, 114: 604,
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** يحمّل صفحة المصحف للسورة. يرجّع مسار الصورة أو null (من غير ما يرمي خطأ). */
export async function fetchMushafPage(surahNumber: number, workDir: string): Promise<string | null> {
  const page = SURAH_START_PAGE[surahNumber];
  if (!page) {
    console.warn(`[mushaf] مفيش صفحة معرّفة للسورة ${surahNumber} — هنستخدم اللوحة بدل المصحف.`);
    return null;
  }
  const p3 = String(page).padStart(3, '0');
  const url = `${MUSHAF_BASE}/${p3}.png`;
  const dest = path.join(workDir, 'mushaf.png');

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length < 20000) throw new Error(`صورة صغيرة/فاسدة (${buf.length}B)`);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, buf);
      console.log(`[mushaf] صفحة ${page} جاهزة للسورة ${surahNumber} (${(buf.length / 1024).toFixed(0)}KB)`);
      return dest;
    } catch (err: any) {
      console.warn(`[mushaf] محاولة ${attempt}/3 فشلت: ${String(err?.message || err).slice(0, 120)}`);
      await sleep(1500 * attempt);
    }
  }
  console.warn('[mushaf] تعذّر تحميل صفحة المصحف — هنكمّل باللوحة بدلها (مش خطأ قاتل).');
  return null;
}
