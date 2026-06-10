/**
 * QEEMA — Image Service (Gemini 2.5 Flash Image)
 * بدّلنا من Leonardo لـ Gemini عشان نستخدم مفاتيحنا الموجودة بدون رصيد منفصل.
 * - يولّد اسكتشات (16:9) + ثمبنايل.
 * - تدوير على المفاتيح التلاتة (نفس منطق llm.ts).
 * - يفشل بصوت عالٍ لو كل المحاولات فشلت.
 */
import { GoogleGenAI } from '@google/genai';
import * as fs from 'fs';
import * as path from 'path';
import { GEMINI_KEYS } from './config.ts';
import { DB } from './db.ts';

const IMAGE_MODEL = 'gemini-2.5-flash-image';
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function isRetryable(msg: string): boolean {
  return /\b(429|500|503)\b|quota|exhaust|rate|unavailable|overloaded|high.demand/i.test(msg);
}

export async function generateImage(
  prompt: string,
  dest: string,
  aspectRatio = '16:9'
): Promise<string> {
  if (GEMINI_KEYS.length === 0) throw new Error('[images] مفيش أي GEMINI_API_KEY متظبّط.');
  fs.mkdirSync(path.dirname(dest), { recursive: true });

  let lastErr: unknown = null;
  const maxRounds = 3;

  for (let round = 1; round <= maxRounds; round++) {
    for (const key of GEMINI_KEYS) {
      // تجاهل المفاتيح المنتهية
      const status = await DB.getKeyStatus(key.name);
      if (status !== 'active') continue;

      try {
        await DB.trackKeyUsage(key.name);
        const ai = new GoogleGenAI({ apiKey: key.value });

        const res = await ai.models.generateContent({
          model: IMAGE_MODEL,
          contents: prompt,
          config: {
            responseModalities: ['IMAGE'],
            imageConfig: { aspectRatio },
          } as any,
        });

        // استخرج البيانات من الرد
        const parts = res.candidates?.[0]?.content?.parts || [];
        let imageData: string | null = null;
        for (const part of parts) {
          if ((part as any).inlineData?.data) {
            imageData = (part as any).inlineData.data;
            break;
          }
        }
        if (!imageData) throw new Error('[images] Gemini رجّع رد بدون بيانات صورة.');

        fs.writeFileSync(dest, Buffer.from(imageData, 'base64'));
        console.log(`[images] جاهزة: ${dest}`);
        return dest;
      } catch (err: any) {
        lastErr = err;
        const msg = String(err?.message || err);
        console.warn(`[images] round ${round} | ${key.name} فشل: ${msg.slice(0, 160)}`);
        if (/\b429\b|quota|exhaust/i.test(msg)) {
          await DB.markKeyExhausted(key.name);
        }
        if (!isRetryable(msg)) {
          // خطأ غير مؤقت (مثلاً policy) — جرّب المفتاح اللي بعده
        }
      }
    }
    // انتهت لفّة كاملة — استنى وجرّب تاني
    if (round < maxRounds) {
      await DB.resetAllKeys();
      const wait = 5000 * round;
      console.warn(`[images] كل المفاتيح فشلت في اللفّة ${round}. انتظار ${wait / 1000}s…`);
      await sleep(wait);
    }
  }

  throw new Error(`[images] فشل توليد الصورة بعد ${maxRounds} لفّات: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}
