/**
 * QEEMA — TTS Pronunciation Gate  ("بوابة النطق")
 * ---------------------------------------------------------------
 * المشكلة الجذرية: العربي من غير تشكيل = غامض. محرّك الصوت بيخمّن الحركات،
 * فبتطلع أخطاء نطق (قِيمة/قَيِّمة، سفينة/سفينت...). القاموس اليدوي بيعالج كلمة كلمة —
 * وده مش حل نظامي.
 *
 * الحل: طبقة تلقائية قبل الصوت — بنخلّي Gemini **يشكّل النص بنطق مصري** (مش يترجمه
 * ولا يغيّره)، وبعدين بنتحقّق برمجيًا إن الكلمات **ما اتغيّرتش** قبل ما نبعتها للصوت.
 *
 * ضمانات (مهمة عشان محتوى ديني):
 *   - النص القرآني عمره ما بيمرّ من هنا (بيتبعت raw من مصدره الموثوق).
 *   - لو الـ LLM غيّر أي كلمة (زوّد/شال/بدّل) -> بنرفض ونرجع النص الأصلي.
 *   - لو المفاتيح/الشبكة فشلت -> بنرجع النص الأصلي (مفيش كسر للبايبلاين).
 *   - قاموس المستخدم بيتطبّق **بعد** البوابة، فتصحيحاتك اليدوية دايمًا أعلى أولوية.
 */
import { GoogleGenAI } from '@google/genai';
import { GEMINI_KEYS, GEMINI_MODEL, TTS_GATE } from './config.ts';

const _cache = new Map<string, string>();

/** شيل التشكيل والتطويل. */
export const stripMarks = (s: string) => s.replace(/[\u064B-\u0652\u0670\u0640]/g, '');

/**
 * "هيكل" الكلمات: بنقارن بيه قبل وبعد عشان نتأكد إن الـ LLM ما غيّرش الكلام.
 * بنسامح في: التشكيل، شكل الألف/الياء، والتاء المربوطة/المفتوحة/الهاء (دي فروق نطق مسموحة).
 */
function skeleton(s: string): string {
  return stripMarks(s)
    .replace(/[أإآٱ]/g, 'ا')
    .replace(/[ىي]/g, 'ي')
    .replace(/[ةته]/g, 'ه')
    .replace(/[^\u0621-\u064A]/g, ''); // بس حروف عربية (بدون مسافات/ترقيم)
}

const SYSTEM = `
أنت مدقّق نطق للغة العربية بلهجة مصرية، بتجهّز نصًا لمحرّك تحويل نص لصوت (TTS).
مهمتك الوحيدة: ترجّع **نفس النص حرفيًا** بعد ما تضيف عليه التشكيل الكامل اللي يخلّي
المحرّك ينطقه **زي المصري ما بينطقه**.

قواعد صارمة:
1) ممنوع تغيّر أي كلمة، أو تزوّد كلمة، أو تشيل كلمة، أو تعيد ترتيب الكلام. نفس الجُمل ونفس الترتيب.
2) ممنوع تترجم أو تفصّح أو تبسّط. النص زي ما هو، بس متشكّل.
3) شكّل بالنطق المصري الحقيقي، مش بالإعراب الفصيح:
   - آخر الكلمة بيتسكّن غالبًا (مش مرفوع/منصوب/مجرور).
   - التاء المربوطة في آخر الكلام بتتنطق هاء ساكنة، وبتتحوّل لـ"ت" لما تكون مضافة
     لكلمة بعدها (مثال: "سفينة قيمة" تبقى "سَفينِت قيمهْ").
   - الجيم مصرية، والقاف زي ما بينطقها المصريين في الكلمة دي (متغيّرش الحروف، بس شكّل صح).
   - راعي الوصل بين الكلمات المتتابعة عشان الإيقاع يبقى طبيعي.
4) استثناء مقدّس: أي لفظ ديني (الله، القرآن، أسماء الله الحسنى، أسماء الأنبياء، آيات)
   شكّله بالنطق الفصيح الصحيح المعروف، ومتعبّش بيه لهجة.
5) علامات الترقيم زي ما هي بالظبط (نقط، فواصل، تعجّب، استفهام) — مهمة لإيقاع الصوت.

المخرجات: JSON فقط بالشكل {"text": "النص المشكَّل"} — من غير أي شرح.
`.trim();

const SCHEMA = {
  type: 'object',
  properties: { text: { type: 'string', description: 'نفس النص بعد التشكيل المصري' } },
  required: ['text'],
};

/** نداء Gemini مع تدوير المفاتيح. يرجّع null عند أي فشل. */
async function callGemini(text: string): Promise<string | null> {
  for (const key of GEMINI_KEYS) {
    try {
      const ai = new GoogleGenAI({ apiKey: key.value });
      const res = await ai.models.generateContent({
        model: GEMINI_MODEL,
        contents: `شكّل النص ده بالنطق المصري:\n"""\n${text}\n"""`,
        config: { systemInstruction: SYSTEM, responseMimeType: 'application/json', responseSchema: SCHEMA },
      });
      const raw = (res.text || '').trim();
      if (!raw) continue;
      const out = JSON.parse(raw)?.text;
      if (typeof out === 'string' && out.trim()) return out.trim();
    } catch (err: any) {
      console.warn(`[tts-gate] ${key.name} فشل: ${String(err?.message || err).slice(0, 110)}`);
    }
  }
  return null;
}

/**
 * يرجّع النص متشكَّلًا بالنطق المصري، أو النص الأصلي لو حصل أي شك.
 * آمن دايمًا: مستحيل يغيّر الكلام.
 */
export async function vocalizeForTts(text: string): Promise<string> {
  const src = (text || '').trim();
  if (!TTS_GATE || !src || GEMINI_KEYS.length === 0) return src;
  if (skeleton(src).length < 4) return src;

  const cached = _cache.get(src);
  if (cached !== undefined) return cached;

  const out = await callGemini(src);
  if (!out) {
    console.warn('[tts-gate] تعذّر التشكيل التلقائي — هنكمّل بالنص الأصلي.');
    _cache.set(src, src);
    return src;
  }

  // التحقّق الحاسم: الكلام لازم يكون هو هو (بس متشكّل)
  if (skeleton(out) !== skeleton(src)) {
    console.warn('[tts-gate] ⚠️ التشكيل غيّر الكلمات — اترفض، وهنستخدم النص الأصلي.');
    _cache.set(src, src);
    return src;
  }

  const added = out.length - src.length;
  console.log(`[tts-gate] تشكيل تلقائي تم (+${added} علامة).`);
  _cache.set(src, out);
  return out;
}
