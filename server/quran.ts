/**
 * QEEMA — Quran Text Service
 * يجيب نص الآيات بالتشكيل من مصدر موثوق (alquran.cloud).
 * نص القرآن لا يُولَّد أبدًا من الـ LLM — الـ LLM يحدد أرقام الآيات فقط، وإحنا نجيب النص.
 */

const EDITION = 'quran-uthmani'; // نص عثماني بالتشكيل
const BASE = 'https://api.alquran.cloud/v1';

/** يجيب كل آيات السورة: رقم الآية -> نصها بالتشكيل. */
export async function fetchSurahAyat(surahNumber: number, retries = 3): Promise<Record<number, string>> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await fetch(`${BASE}/surah/${surahNumber}/${EDITION}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: any = await res.json();
      const ayat = data?.data?.ayahs;
      if (!Array.isArray(ayat) || ayat.length === 0) throw new Error('رد بدون آيات');
      const map: Record<number, string> = {};
      for (const a of ayat) map[a.numberInSurah] = String(a.text || '').trim();
      return map;
    } catch (err) {
      lastErr = err;
      if (attempt < retries) await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
  throw new Error(`[quran] فشل جلب نص سورة ${surahNumber}: ${String((lastErr as Error)?.message || lastErr)}`);
}

/**
 * ينظّف نص الآية للنطق بصوت الشرح:
 * - يشيل علامات الوقف/التجويد الصغيرة (ۖ ۗ ۚ ۛ ۜ ۤ ۨ ۩ والأرقام) اللي بتلخبط محرّك الصوت.
 * - يحافظ على الحركات الأساسية (فتحة/ضمة/كسرة/شدة/سكون) عشان النطق يطلع صح.
 */
export function cleanAyahForTts(text: string): string {
  // نحافظ على التشكيل الكامل (الحركات) عشان النطق يطلع فصيح صحيح.
  // نشيل فقط: علامات الوقف/التجويد الصغيرة، رمز نهاية الآية، وأرقام الآيات.
  return text
    .replace(/[\u06D6-\u06DC\u06DE-\u06E4\u06E7-\u06ED]/g, '') // علامات وقف/تجويد (مش الحركات)
    .replace(/\u06DD/g, '')                 // رمز نهاية الآية ۝
    .replace(/[\u0660-\u0669\u06F0-\u06F9]/g, '') // أرقام عربية
    .replace(/\s+/g, ' ')
    .trim();
}

/** يجمع نص نطاق آيات (start..end) منظّفًا للنطق. */
export function ayahRangeForTts(map: Record<number, string>, start: number, end: number): string {
  const parts: string[] = [];
  for (let n = start; n <= end; n++) {
    if (map[n]) parts.push(cleanAyahForTts(map[n]));
  }
  return parts.join('. ').trim();
}
