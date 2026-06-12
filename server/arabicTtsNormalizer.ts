// arabicTtsNormalizer.ts
// ---------------------------------------------------------------------------
// QEEMA — طبقة تطبيع النطق قبل ElevenLabs (diacritic-safe).
// بتحل مشكلة نطق الآيات أثناء الشرح:
//   1) اللام الشمسية: "الشمس" -> "اشّمس"  (نشيل لام التعريف ونشدّد الحرف الشمسي)
//   2) لفظ الجلالة:   "اللَّهِ" -> "اللّاهِ" (لام مشدّدة بدل ما تتنطق منفصلة، مع الحفاظ على حركة الآخر)
//
// بتشتغل على النص المشكّل (raw: true) من غير ما تخرّب حركات الآية،
// وعلى النص العادي (بعد stripTashkeel) برضه.
//
// ملاحظة: tags النطق (IPA/CMU) في ElevenLabs للإنجليزي بس؛ للعربي بنستخدم
// إعادة كتابة صوتية (alias) = نكتب الكلمة بالطريقة اللي تتنطق بيها.
// ---------------------------------------------------------------------------

const SHADDA = '\u0651'; // ّ
// كل علامات التشكيل/الإعجام العربية (حركات + شدّة + ألف خنجرية + علامات قرآنية).
const DIAC = /[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]/;

// بادئات تُلصق بأل التعريف (و ف ب ك).
const PREFIX = new Set(['و', 'ف', 'ب', 'ك']);

// حروف شمسية ما عدا اللام (نستثني اللام عشان نتجنّب لخبطة "الل...").
const SUN_NO_LAM = new Set('تثدذرزسشصضطظن'.split(''));

// كلمات بادئة بـ"ال" واللام فيها لازم تتنطق (أسماء موصولة) — متتمسّش.
const KEEP_LAAM = new Set([
  'الذي', 'التي', 'الذين', 'اللاتي', 'اللذان', 'اللتان', 'اللواتي', 'اللائي',
]);

// لفظ الجلالة وصيغه — إعادة كتابة صوتية (alias) بلام مشدّدة.
// ⚠️ candidates للاختبار على صوتك: جرّب "اللّاه" / "أللاه" / "اَللَّاه" وعدّل.
const JALALA: Record<string, string> = {
  'الله': 'اللّاه',
  'والله': 'واللّاه',
  'بالله': 'باللّاه',
  'تالله': 'تاللّاه',
  'فالله': 'فاللّاه',
  'لله': 'لِللّاه',
  'اللهم': 'اللّاهُمّ',
};

/** تقسيم الكلمة لوحدات: كل وحدة = حرف أساسي + علامات التشكيل اللي بعده. */
function splitUnits(word: string): string[] {
  const units: string[] = [];
  for (const ch of word) {
    if (DIAC.test(ch) && units.length) units[units.length - 1] += ch;
    else units.push(ch);
  }
  return units;
}

/** لفظ الجلالة: نرجّع الصيغة الصوتية ونلصق حركة آخر الكلمة الأصلية. */
function tryJalala(units: string[], bare: string): string | null {
  const alias = JALALA[bare];
  if (!alias) return null;
  const tail = units[units.length - 1].slice(1); // حركات آخر حرف
  return bare !== 'اللهم' && tail ? alias + tail : alias;
}

/** اللام الشمسية: نشيل لام التعريف ونشدّد الحرف الشمسي، مع الحفاظ على التشكيل. */
function trySun(units: string[]): string | null {
  let i = 0;
  const pre: string[] = [];
  if (units[i] && PREFIX.has(units[i][0])) pre.push(units[i++]);
  if (!units[i] || units[i][0] !== 'ا') return null;
  const alef = units[i++];
  if (!units[i] || units[i][0] !== 'ل') return null;
  i++; // نتخطّى لام التعريف (وتشكيلها) — بتتشال
  if (!units[i] || !SUN_NO_LAM.has(units[i][0])) return null;
  let sun = units[i++];
  if (!sun.includes(SHADDA)) sun = sun[0] + SHADDA + sun.slice(1); // نضمن الشدّة
  return [...pre, alef, sun, ...units.slice(i)].join('');
}

function normalizeWord(word: string): string {
  const units = splitUnits(word);
  const bare = units.map((u) => u[0]).join('');
  return tryJalala(units, bare) ?? (KEEP_LAAM.has(bare) ? word : trySun(units) ?? word);
}

/**
 * طبّع نص عربي قبل إرساله لـ ElevenLabs.
 * آمنة على النص المشكّل والعادي. نادِها داخل synthesize() في voice.ts.
 */
export function normalizeArabicForTts(text: string): string {
  return text.replace(/[\u0621-\u064A][\u0621-\u065F\u0670\u06D6-\u06ED]*/g, normalizeWord);
}

// أمثلة مُختبَرة:
//  "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ" => "بِسْمِ اللّاهِ ارَّحْمَٰنِ ارَّحِيمِ"
//  "قُلْ هُوَ اللَّهُ أَحَدٌ"            => "قُلْ هُوَ اللّاهُ أَحَدٌ"
//  "الذي خلق"                          => "الذي خلق"  (اسم موصول — متغيّرش)
