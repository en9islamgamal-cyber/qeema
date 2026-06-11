/**
 * QEEMA — LLM Service (Gemini)  v2 — تدوير أمتن
 * - يلف على المفاتيح التلاتة في أي خطأ مؤقت (503/500/UNAVAILABLE/429/quota).
 * - انتظار تصاعدي بين المحاولات (للزحام المؤقت من Google).
 * - محاولات موزّعة: كل مفتاح ياخد فرصته قبل الفشل النهائي.
 */
import { GoogleGenAI } from '@google/genai';
import { GEMINI_KEYS, GEMINI_MODEL } from './config.ts';
import { DB } from './db.ts';
import {
  SurahInput,
  EpisodePlan,
  buildEpisodePlanPrompt,
  buildTitlePrompt,
} from './prompts.ts';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** أخطاء مؤقتة تستاهل نجرّب مفتاح تاني / نعيد المحاولة. */
function isRetryable(msg: string): boolean {
  return /\b(429|500|503)\b|quota|exhaust|rate|unavailable|overloaded|high demand|internal/i.test(msg);
}
/** أخطاء حصّة فعلية (تستاهل نعلّم المفتاح كمنتهي). */
function isQuota(msg: string): boolean {
  return /\b429\b|quota|exhaust|resource_exhausted/i.test(msg);
}

async function nextActiveKey(skip: Set<string>): Promise<{ value: string; name: string } | null> {
  for (const k of GEMINI_KEYS) {
    if (skip.has(k.name)) continue;
    const status = await DB.getKeyStatus(k.name);
    if (status === 'active') return { value: k.value, name: k.name };
  }
  return null;
}

async function generateJSON<T>(
  prompt: { system: string; user: string },
  schema: any,
  episodeId: string | null,
  maxRounds = 3 // كل round = لفّة كاملة على كل المفاتيح
): Promise<T> {
  if (GEMINI_KEYS.length === 0) throw new Error('[llm] مفيش أي GEMINI_API_KEY متظبّط.');

  let lastErr: unknown = null;

  for (let round = 1; round <= maxRounds; round++) {
    const triedThisRound = new Set<string>();

    // جرّب كل مفتاح متاح في اللفّة دي
    for (let i = 0; i < GEMINI_KEYS.length; i++) {
      let key = await nextActiveKey(triedThisRound);
      // لو كله متعلّم "منتهي"، صفّرهم وجرّب من الأول
      if (!key) {
        await DB.resetAllKeys();
        key = await nextActiveKey(triedThisRound);
      }
      if (!key) break;
      triedThisRound.add(key.name);

      try {
        await DB.trackKeyUsage(key.name);
        const ai = new GoogleGenAI({ apiKey: key.value });
        const res = await ai.models.generateContent({
          model: GEMINI_MODEL,
          contents: prompt.user,
          config: {
            systemInstruction: prompt.system,
            responseMimeType: 'application/json',
            responseSchema: schema,
          },
        });
        const text = (res.text || '').trim();
        if (!text) throw new Error('Gemini رجّع نص فاضي.');
        return JSON.parse(text) as T;
      } catch (err: any) {
        lastErr = err;
        const msg = String(err?.message || err);
        console.warn(`[llm] round ${round} | ${key.name} فشل: ${msg.slice(0, 160)}`);
        if (isQuota(msg)) {
          await DB.markKeyExhausted(key.name); // 429 -> علّمه منتهي وانتقل فورًا للي بعده
        } else if (isRetryable(msg)) {
          // 503/500/زحام -> مش غلطة المفتاح؛ جرّب اللي بعده على طول في نفس اللفّة
        } else {
          // خطأ غير متوقّع (مثلاً JSON بايظ) -> جرّب اللي بعده برضه
        }
      }
    }

    // خلصت لفّة كاملة على كل المفاتيح من غير نجاح -> استنى وزِد الانتظار قبل لفّة جديدة
    const wait = 5000 * round; // 5s, 10s, 15s
    console.warn(`[llm] كل المفاتيح فشلت في اللفّة ${round}. انتظار ${wait / 1000}s قبل لفّة جديدة…`);
    await sleep(wait);
  }

  throw new Error(`[llm] فشل توليد JSON بعد ${maxRounds} لفّات على كل المفاتيح: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}

export async function generateEpisodePlan(surah: SurahInput, episodeId: string, totalAyat: number): Promise<EpisodePlan> {
  const p = buildEpisodePlanPrompt(surah, totalAyat);
  const plan = await generateJSON<EpisodePlan>(p, p.schema, episodeId);
  if (!plan?.intro || !Array.isArray(plan.ideas) || plan.ideas.length < 3) {
    throw new Error('[llm] الخطة المرجّعة ناقصة (لازم intro + 3 ideas على الأقل).');
  }
  return plan;
}

export interface TitleResult {
  title: string;
  theme: string;
  description: string;
  tags: string[];
}

export async function generateTitle(plan: EpisodePlan, surah: SurahInput, episodeId: string): Promise<TitleResult> {
  const p = buildTitlePrompt(plan, surah);
  return generateJSON<TitleResult>(p, p.schema, episodeId);
}
