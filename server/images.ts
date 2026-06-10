/**
 * QEEMA — Image Service (Leonardo.ai)
 * - يولّد اسكتشات (1280x720) + ثمبنايل.
 * - Leonardo max width = 1536px. نستخدم 1280x720 وFFmpeg يكبّرها لـ 1920x1080.
 * - يفشل بصوت عالٍ. مفيش mock أو placeholder.
 */
import * as fs from 'fs';
import * as path from 'path';
import { LEONARDO } from './config.ts';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Leonardo max = 1536px → نستخدم 1280x720 (16:9)
const LEO_W = 1280, LEO_H = 720;

async function createGeneration(prompt: string, width: number, height: number): Promise<string> {
  const res = await fetch(`${LEONARDO.baseUrl}/generations`, {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      authorization: `Bearer ${LEONARDO.apiKey()}`,
    },
    body: JSON.stringify({
      modelId: LEONARDO.modelId,
      prompt: prompt.slice(0, 1490),
      width,
      height,
      num_images: 1,
      public: false,
    }),
  });
  if (!res.ok) {
    const t = await res.text().catch(() => '');
    throw new Error(`[images] فشل إنشاء generation: HTTP ${res.status} ${t.slice(0, 300)}`);
  }
  const data: any = await res.json();
  const id = data?.sdGenerationJob?.generationId;
  if (!id) throw new Error(`[images] مفيش generationId في الرد: ${JSON.stringify(data).slice(0, 300)}`);
  return id;
}

async function pollImageUrl(generationId: string, maxTries = 40): Promise<string> {
  for (let i = 0; i < maxTries; i++) {
    await sleep(3000);
    const res = await fetch(`${LEONARDO.baseUrl}/generations/${generationId}`, {
      headers: { accept: 'application/json', authorization: `Bearer ${LEONARDO.apiKey()}` },
    });
    if (!res.ok) continue;
    const data: any = await res.json();
    const gen = data?.generations_by_pk;
    if (gen?.status === 'COMPLETE') {
      const url = gen?.generated_images?.[0]?.url;
      if (!url) throw new Error('[images] اكتمل التوليد بدون صورة.');
      return url;
    }
    if (gen?.status === 'FAILED') throw new Error('[images] فشل التوليد على Leonardo.');
  }
  throw new Error(`[images] انتهى وقت الانتظار للـ generation ${generationId}`);
}

async function download(url: string, dest: string): Promise<void> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`[images] فشل تنزيل الصورة: HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.length < 1000) throw new Error('[images] صورة صغيرة/فاسدة.');
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, buf);
}

export async function generateImage(
  prompt: string,
  dest: string,
  w = LEO_W,
  h = LEO_H
): Promise<string> {
  console.log(`[images] توليد صورة -> ${path.basename(dest)}`);
  const genId = await createGeneration(prompt, w, h);
  const url = await pollImageUrl(genId);
  await download(url, dest);
  console.log(`[images] جاهزة: ${dest}`);
  return dest;
}
