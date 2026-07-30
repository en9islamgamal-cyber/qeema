/**
 * QEEMA — Tafsir Service
 * بيجيب تفسير الآيات من مصدر موثوق (الميسّر أساسي + السعدي توسّع) عبر spa5k/tafsir_api.
 *
 * الهدف: الذكاء الاصطناعي *مايفسّرش* — بس يبسّط ويربط تفسيرًا موثوقًا بنديه له.
 * فبنجيب التفسير هنا ونحقنه في برومبت الخطة كمصدر إجباري.
 *
 * المصدر: https://cdn.jsdelivr.net/gh/spa5k/tafsir_api@main/tafsir/{edition}/{surah}/{ayah}.json
 *  - الميسّر: ar-tafsir-muyassar
 *  - السعدي: ar-tafseer-al-saddi
 */
import { TAFSIR_BASE, TAFSIR_PRIMARY, TAFSIR_SECONDARY } from './config.ts';

export interface AyahTafsir {
  ayah: number;
  primary: string;      // الميسّر
  secondary?: string;   // السعدي (لو متاح)
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const _cache = new Map<string, string>();

/** جلب تفسير آية واحدة من إصدار معيّن (مع كاش وإعادة محاولة). */
async function fetchEditionAyah(edition: string, surah: number, ayah: number): Promise<string> {
  const cacheKey = `${edition}:${surah}:${ayah}`;
  const cached = _cache.get(cacheKey);
  if (cached !== undefined) return cached;

  const url = `${TAFSIR_BASE}/${edition}/${surah}/${ayah}.json`;
  let lastErr: unknown = null;

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: any = await res.json();
      // شكل spa5k: { text, ayah, surah } — مع fallback لأسماء حقول محتملة
      const text = String(data?.text ?? data?.tafsir ?? data?.tafseer ?? '')
        .replace(/<[^>]+>/g, ' ')   // شيل أي HTML
        .replace(/\s+/g, ' ')
        .trim();
      _cache.set(cacheKey, text);
      return text;
    } catch (err) {
      lastErr = err;
      await sleep(400 * attempt);
    }
  }
  throw new Error(`[tafsir] فشل جلب ${edition} ${surah}:${ayah} — ${String((lastErr as Error)?.message || lastErr)}`);
}

/**
 * جلب تفسير نطاق آيات. الميسّر إجباري (لو فشل بيرمي خطأ)،
 * والسعدي ثانوي (لو غاب بنكمّل من غيره).
 */
export async function fetchTafsir(surah: number, ayahStart: number, ayahEnd: number): Promise<AyahTafsir[]> {
  const out: AyahTafsir[] = [];
  for (let a = ayahStart; a <= ayahEnd; a++) {
    const primary = await fetchEditionAyah(TAFSIR_PRIMARY, surah, a);
    let secondary: string | undefined;
    if (TAFSIR_SECONDARY) {
      try { secondary = await fetchEditionAyah(TAFSIR_SECONDARY, surah, a); } catch { /* السعدي ثانوي؛ نتجاهل لو غاب */ }
    }
    out.push({ ayah: a, primary, secondary });
  }
  if (out.every((t) => !t.primary)) {
    throw new Error('[tafsir] مفيش أي تفسير رجع من المصدر — بلاش نكمّل عشان ما نسيبش الـ AI يجتهد.');
  }
  return out;
}

/** تنسيق التفسير لحقنه في البرومبت كمصدر مرجعي. */
export function formatTafsirForPrompt(items: AyahTafsir[]): string {
  return items
    .map((t) => {
      let s = `【الآية ${t.ayah}】\nالتفسير المعتمد (الميسّر): ${t.primary || '(غير متوفّر)'}`;
      if (t.secondary) s += `\nتوسّع (السعدي): ${t.secondary}`;
      return s;
    })
    .join('\n\n');
}
