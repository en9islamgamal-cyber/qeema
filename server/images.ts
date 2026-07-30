/**
 * QEEMA — Image Service (Gemini 2.5 Flash Image)
 * - بيولّد الصور عبر Gemini بدل Hugging Face (اللي شال موديل FLUX من hf-inference).
 * - تدوير على مفاتيح Gemini التلاتة (نفس مفاتيح النص = أرصدة منفصلة).
 * - 429/quota -> المفتاح اللي بعده. 500/503 -> إعادة على نفس المفتاح.
 * - رد من غير صورة (فلتر محتوى) -> محاولة تانية ثم المفتاح اللي بعده.
 * - يفشل بصوت عالٍ. مفيش mock.
 */
import * as fs from 'fs';
import * as path from 'path';
import { GoogleGenAI } from '@google/genai';
import { GEMINI_KEYS } from './config.ts';

// موديل الصور — قابل للتغيير من secret لو حبيت (مثلاً gemini-2.0-flash-preview-image-generation).
const IMAGE_MODEL = process.env['GEMINI_IMAGE_MODEL'] || 'gemini-2.5-flash-image';
const IMAGE_ASPECT = process.env['IMAGE_ASPECT'] || '16:9';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** خطأ حصّة -> المفتاح ده خلص، انتقل للي بعده. */
function isQuota(msg: string): boolean {
  return /\b429\b|quota|exhaust|resource_exhausted|rate limit/i.test(msg);
}
/** خطأ مؤقت (زحام) -> استنى وأعد على نفس المفتاح. */
function isRetryable(msg: string): boolean {
  return /\b(500|503)\b|unavailable|overloaded|internal|high demand/i.test(msg);
}

/** يستخرج أول صورة (base64) من رد Gemini ويحفظها. يرجّع true لو نجح. */
function saveImageFromResponse(res: any, dest: string): boolean {
  const parts = res?.candidates?.[0]?.content?.parts || [];
  for (const part of parts) {
    const inline = part?.inlineData || part?.inline_data;
    if (inline?.data) {
      const buf = Buffer.from(inline.data, 'base64');
      if (buf.length < 1000) continue;
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, buf);
      console.log(`[images] جاهزة: ${dest} (${(buf.length / 1024).toFixed(0)}KB)`);
      return true;
    }
  }
  return false;
}

export async function generateImage(prompt: string, dest: string): Promise<string> {
  if (GEMINI_KEYS.length === 0) throw new Error('[images] مفيش أي GEMINI_API_KEY متظبّط.');
  let lastErr: unknown = null;

  for (let k = 0; k < GEMINI_KEYS.length; k++) {
    const key = GEMINI_KEYS[k];
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const ai = new GoogleGenAI({ apiKey: key.value });
        const res = await ai.models.generateContent({
          model: IMAGE_MODEL,
          contents: prompt.slice(0, 2000),
          config: {
            responseModalities: ['IMAGE'],
            // لو ظهر خطأ عن imageConfig/aspectRatio، امسح السطر ده وبس.
            imageConfig: { aspectRatio: IMAGE_ASPECT },
          },
        });
        if (saveImageFromResponse(res, dest)) return dest;
        throw new Error('الرد مفهوش صورة (ممكن فلتر محتوى).');
      } catch (err: any) {
        lastErr = err;
        const msg = String(err?.message || err);
        console.warn(`[images] ${key.name} محاولة ${attempt}/3 فشلت: ${msg.slice(0, 150)}`);
        if (isQuota(msg)) break;                       // المفتاح خلص -> اللي بعده
        if (isRetryable(msg)) { await sleep(5000 * attempt); continue; } // زحام -> أعد
        await sleep(1500 * attempt);                    // خطأ تاني (مثلاً مفيش صورة) -> محاولة تانية
      }
    }
  }
  throw new Error(`[images] فشل توليد الصورة على كل مفاتيح Gemini: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}
