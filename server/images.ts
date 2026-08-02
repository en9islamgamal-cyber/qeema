/**
 * QEEMA — Image Service (Cloudflare Workers AI — FLUX.1-schnell)
 * مصدر مجاني ومستقر (طبقة مجانية ~10k Neurons/يوم = مئات الصور).
 * بديل HF (اتوقف) وGemini (بقى مدفوع) وPollinations (غير مستقر).
 *
 * محتاج secrets:
 *   CLOUDFLARE_ACCOUNT_ID   — من داشبورد Cloudflare
 *   CLOUDFLARE_API_TOKEN    — توكن بصلاحية Workers AI
 * اختياري:
 *   CF_IMAGE_MODEL   (افتراضي @cf/black-forest-labs/flux-1-schnell)
 *   CF_IMAGE_STEPS   (1-8، افتراضي 6)
 */
import * as fs from 'fs';
import * as path from 'path';

const ACCOUNT = process.env['CLOUDFLARE_ACCOUNT_ID'] || '';
const TOKEN = process.env['CLOUDFLARE_API_TOKEN'] || '';
const MODEL = process.env['CF_IMAGE_MODEL'] || '@cf/black-forest-labs/flux-1-schnell';
const STEPS = Math.min(8, Math.max(1, parseInt(process.env['CF_IMAGE_STEPS'] || '6', 10)));

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function generateImage(prompt: string, dest: string): Promise<string> {
  if (!ACCOUNT || !TOKEN) {
    throw new Error('[images] محتاج secrets: CLOUDFLARE_ACCOUNT_ID و CLOUDFLARE_API_TOKEN.');
  }
  const url = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT}/ai/run/${MODEL}`;
  const clean = prompt.replace(/\s+/g, ' ').trim().slice(0, 2000);
  let lastErr: unknown = null;

  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
        body: JSON.stringify({ prompt: clean, steps: STEPS, seed: Math.floor(Math.random() * 1e9) }),
      });

      if ([429, 500, 502, 503, 504].includes(res.status)) {
        console.warn(`[images] Cloudflare مشغول (HTTP ${res.status}) — إعادة محاولة ${attempt}/4…`);
        await sleep(5000 * attempt);
        continue;
      }

      const data: any = await res.json().catch(() => null);
      if (!res.ok || !data?.success) {
        const errMsg = data?.errors ? JSON.stringify(data.errors).slice(0, 180) : `HTTP ${res.status}`;
        throw new Error(errMsg);
      }

      const b64 = data?.result?.image;
      if (!b64) throw new Error('الرد مفهوش صورة (result.image فاضي).');
      const buf = Buffer.from(b64, 'base64');
      if (buf.length < 1500) throw new Error(`صورة صغيرة/فاسدة (${buf.length}B)`);

      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, buf);
      console.log(`[images] جاهزة: ${dest} (${(buf.length / 1024).toFixed(0)}KB)`);
      return dest;
    } catch (err) {
      lastErr = err;
      console.warn(`[images] محاولة ${attempt}/4 فشلت: ${String((err as Error)?.message || err).slice(0, 150)}`);
      await sleep(3000 * attempt);
    }
  }
  throw new Error(`[images] فشل توليد الصورة من Cloudflare: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}
