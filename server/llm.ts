/**
 * QEEMA — LLM Service (Gemini)
 * - تدوير 3 مفاتيح Gemini عبر api_key_metrics.
 * - تنفّذ برومبتات قيمة وترجّع JSON مُهيكل (EpisodePlan / Title).
 */
import { GoogleGenAI } from '@google/genai';
import { GEMINI_KEYS, GEMINI_MODEL } from './config.ts';
import { DB } from './db.ts';
import {
  SurahInput,
  EpisodePlan,
  EPISODE_PLAN_SCHEMA,
  TITLE_SCHEMA,
  buildEpisodePlanPrompt,
  buildTitlePrompt,
} from './prompts.ts';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function pickActiveKey(): Promise<{ value: string; name: string }> {
  if (GEMINI_KEYS.length === 0) throw new Error('[llm] مفيش أي GEMINI_API_KEY متظبّط.');
  for (const k of GEMINI_KEYS) {
    if ((await DB.getKeyStatus(k.name)) === 'active') return { value: k.value, name: k.name };
  }
  // كلهم منتهيين -> صفّرهم وابدأ من الأول
  console.warn('[llm] كل مفاتيح Gemini منتهية — إعادة تصفير.');
  await DB.resetAllKeys();
  return { value: GEMINI_KEYS[0].value, name: GEMINI_KEYS[0].name };
}

async function generateJSON<T>(
  prompt: { system: string; user: string },
  schema: any,
  episodeId: string | null,
  retries = 4,
  backoff = 2000
): Promise<T> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= retries; attempt++) {
    const key = await pickActiveKey();
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
      const isQuota = /429|quota|exhaust|rate/i.test(msg);
      console.warn(`[llm] محاولة ${attempt}/${retries} فشلت (${key.name}): ${msg}`);
      if (isQuota) await DB.markKeyExhausted(key.name);
      else await sleep(backoff * attempt);
    }
  }
  throw new Error(`[llm] فشل توليد JSON بعد ${retries} محاولات: ${String((lastErr as Error)?.message || lastErr)}`);
}

export async function generateEpisodePlan(surah: SurahInput, episodeId: string): Promise<EpisodePlan> {
  const p = buildEpisodePlanPrompt(surah);
  const plan = await generateJSON<EpisodePlan>(p, p.schema, episodeId);
  if (!plan?.intro || !Array.isArray(plan.ideas) || plan.ideas.length < 3) {
    throw new Error('[llm] الخطة المرجّعة ناقصة (intro/ideas).');
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
