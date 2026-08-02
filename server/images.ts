/**
 * QEEMA — Image Service (Pollinations — Flux مجاني)
 * - Flux على Pollinations مجاني وبدون مفتاح (HF شال الموديل، وصور Gemini بقت مدفوعة).
 * - GET على image.pollinations.ai/prompt/{prompt} -> بيرجّع الصورة مباشرة.
 * - إعادة محاولة على الزحام/الأخطاء المؤقتة مع احترام حد السرعة المجاني.
 * - يفشل بصوت عالٍ. مفيش mock.
 *
 * إعدادات اختيارية (secrets):
 *   IMAGE_MODEL           (افتراضي flux)
 *   IMAGE_WIDTH/HEIGHT    (افتراضي 1024x576 = 16:9)
 *   POLLINATIONS_TOKEN    (مفتاح مجاني من enter.pollinations.ai لو حبيت سرعة/موثوقية أعلى)
 *   POLLINATIONS_BASE     (لو حبيت تغيّر لـ https://gen.pollinations.ai/image)
 */
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env['POLLINATIONS_BASE'] || 'https://image.pollinations.ai/prompt';
const MODEL = process.env['IMAGE_MODEL'] || 'flux';
const TOKEN = process.env['POLLINATIONS_TOKEN'] || '';
const IMG_W = parseInt(process.env['IMAGE_WIDTH'] || '1024', 10);
const IMG_H = parseInt(process.env['IMAGE_HEIGHT'] || '576', 10);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function generateImage(prompt: string, dest: string): Promise<string> {
  const clean = prompt.replace(/\s+/g, ' ').trim().slice(0, 1500);
  const params = new URLSearchParams({
    model: MODEL,
    width: String(IMG_W),
    height: String(IMG_H),
    seed: String(Math.floor(Math.random() * 1e9)),
    nologo: 'true',
    safe: 'true',
  });
  const url = `${BASE}/${encodeURIComponent(clean)}?${params.toString()}`;
  const headers: Record<string, string> = {};
  if (TOKEN) headers['authorization'] = `Bearer ${TOKEN}`;

  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const res = await fetch(url, { headers });
      if ([429, 500, 502, 503, 504].includes(res.status)) {
        console.warn(`[images] Pollinations مشغول (HTTP ${res.status}) — إعادة محاولة ${attempt}/5…`);
        await sleep(7000 * attempt); // حد السرعة المجاني ~1 كل 15ث
        continue;
      }
      if (!res.ok) {
        const t = await res.text().catch(() => '');
        throw new Error(`HTTP ${res.status}: ${t.slice(0, 150)}`);
      }
      const ct = res.headers.get('content-type') || '';
      if (!ct.startsWith('image/')) {
        const t = await res.text().catch(() => '');
        throw new Error(`رد مش صورة (${ct}): ${t.slice(0, 120)}`);
      }
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length < 1500) throw new Error(`صورة صغيرة/فاسدة (${buf.length}B)`);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.writeFileSync(dest, buf);
      console.log(`[images] جاهزة: ${dest} (${(buf.length / 1024).toFixed(0)}KB)`);
      return dest;
    } catch (err) {
      lastErr = err;
      console.warn(`[images] محاولة ${attempt}/5 فشلت: ${String((err as Error)?.message || err).slice(0, 140)}`);
      await sleep(5000 * attempt);
    }
  }
  throw new Error(`[images] فشل توليد الصورة من Pollinations: ${String((lastErr as Error)?.message || lastErr).slice(0, 200)}`);
}
