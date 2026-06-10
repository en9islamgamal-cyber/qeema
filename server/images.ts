/**
 * QEEMA — Image Service (Hugging Face — FLUX.1-schnell)
 * - تدوير على 3 مفاتيح HF (حسابات مختلفة = أرصدة منفصلة).
 * - لو مفتاح رجّع خطأ رصيد/حصّة (402/429) -> يجرّب اللي بعده.
 * - 503 (الموديل بيتحمّل) -> يستنى ويعيد على نفس المفتاح.
 * - يفشل بصوت عالٍ. مفيش mock.
 */
import * as fs from 'fs';
import * as path from 'path';
import { HF_KEYS } from './config.ts';

const HF_MODEL = process.env['HF_IMAGE_MODEL'] || 'black-forest-labs/FLUX.1-schnell';
const HF_URL = `https://router.huggingface.co/hf-inference/models/${HF_MODEL}`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** خطأ رصيد/حصّة -> المفتاح ده خلص، انتقل للي بعده. */
function isQuota(status: number, msg: string): boolean {
  return status === 402 || status === 429 || /quota|credit|limit|exceeded|payment/i.test(msg);
}

async function tryOneKey(key: string, prompt: string, dest: string): Promise<'ok' | 'quota' | 'retry'> {
  const res = await fetch(HF_URL, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${key}`,
      'content-type': 'application/json',
      accept: 'image/png',
    },
    body: JSON.stringify({
      inputs: prompt.slice(0, 1900),
      parameters: { width: 1024, height: 576 }, // 16:9 تقريبًا
    }),
  });

  if (res.status === 503) {
    console.warn('[images] الموديل بيتحمّل (503)…');
    return 'retry';
  }
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    if (isQuota(res.status, t)) {
      console.warn(`[images] المفتاح ده خلص رصيده (HTTP ${res.status}) — تجربة المفتاح اللي بعده.`);
      return 'quota';
    }
    throw new Error(`HTTP ${res.status}: ${t.slice(0, 250)}`);
  }

  const ct = res.headers.get('content-type') || '';
  if (!ct.startsWith('image/')) {
    const t = await res.text().catch(() => '');
    if (isQuota(200, t)) return 'quota';
    throw new Error(`رد مش صورة (${ct}): ${t.slice(0, 200)}`);
  }

  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.length < 1000) throw new Error(`صورة صغيرة/فاسدة (${buf.length}B)`);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, buf);
  console.log(`[images] جاهزة: ${dest} (${(buf.length / 1024).toFixed(0)}KB)`);
  return 'ok';
}

export async function generateImage(prompt: string, dest: string): Promise<string> {
  if (HF_KEYS.length === 0) throw new Error('[images] مفيش أي HF_API_KEY متظبّط.');
  let lastErr: unknown = null;

  // جرّب كل مفتاح؛ مع إعادة محاولة للـ 503 (cold start) على كل مفتاح
  for (let k = 0; k < HF_KEYS.length; k++) {
    const key = HF_KEYS[k];
    for (let attempt = 1; attempt <= 4; attempt++) {
      try {
        const result = await tryOneKey(key.value, prompt, dest);
        if (result === 'ok') return dest;
        if (result === 'quota') break;           // المفتاح خلص -> المفتاح اللي بعده
        if (result === 'retry') {                 // 503 -> استنى وأعد على نفس المفتاح
          await sleep(8000 * attempt);
          continue;
        }
      } catch (err) {
        lastErr = err;
        console.warn(`[images] ${key.name} محاولة ${attempt}/4 فشلت: ${String((err as Error)?.message || err).slice(0, 140)}`);
        await sleep(4000 * attempt);
      }
    }
  }
  throw new Error(`[images] فشل توليد الصورة على كل مفاتيح HF: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}
