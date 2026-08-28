/**
 * QEEMA (قيمة) — Central Configuration
 * كل المفاتيح والإعدادات في مكان واحد. القيم الحسّاسة من متغيرات البيئة (GitHub Secrets).
 */
import * as path from 'path';

function required(name: string): string {
  const v = process.env[name];
  if (!v || !v.trim()) {
    throw new Error(`[config] متغير البيئة المطلوب مفقود: ${name}`);
  }
  return v.trim();
}
function optional(name: string, fallback = ''): string {
  return (process.env[name] || fallback).trim();
}

/* ---------- مزوّد النص (Gemini) ---------- */
export const GEMINI_MODEL = optional('GEMINI_MODEL', 'gemini-2.5-flash');
export const GEMINI_KEYS = [
  { name: 'KeyA', value: optional('GEMINI_API_KEY') },
  { name: 'KeyB', value: optional('GEMINI_API_KEY_2') },
  { name: 'KeyC', value: optional('GEMINI_API_KEY_3') },
].filter((k) => k.value);

/* ---------- الصور (Hugging Face — FLUX) ---------- */
export const HF_KEYS = [
  { name: 'HF_A', value: optional('HF_API_KEY') },
  { name: 'HF_B', value: optional('HF_API_KEY_2') },
  { name: 'HF_C', value: optional('HF_API_KEY_3') },
].filter((k) => k.value);

/* ---------- الصوت (ElevenLabs) ---------- */
export const ELEVENLABS = {
  apiKey: () => required('ELEVENLABS_API_KEY'),
  // ⚠️ تأكّد إن ده الـ voice id بتاعك بالظبط — أو حطّه في secret اسمه ELEVENLABS_VOICE_ID
  voiceId: optional('ELEVENLABS_VOICE_ID', 'vWDp3PLsTWjIhBxxUKh9'),
  modelId: optional('ELEVENLABS_MODEL_ID', 'eleven_multilingual_v2'),
  stability: 0.4,        // 0.4 = توازن بشري (واطي زيادة بيسبب أخطاء نطق)
  similarityBoost: 0.8,
  style: 0.2,            // style واطي = أطبع وأقل روبوتية
  useSpeakerBoost: true,
};

/* ---------- الصور (Leonardo) ---------- */
export const LEONARDO = {
  apiKey: () => required('LEONARDO_API_KEY'),
  // موديل افتراضي عام؛ غيّره من حسابك (List Platform Models) لو حبيت ستايل تاني.
  // aa77f04e... = Leonardo Kino XL.
  modelId: optional('LEONARDO_MODEL_ID', 'aa77f04e-3eec-4034-9c07-d0f619684628'),
  baseUrl: 'https://cloud.leonardo.ai/api/rest/v1',
};

/* ---------- التلاوة (everyayah) ---------- */
export const EVERYAYAH_BASE = 'https://everyayah.com/data';
export const RECITER = optional('RECITER', 'MaherAlMuaiqly128kbps');
export const RECITATION_TEMPO = parseFloat(optional('RECITATION_TEMPO', '1.0'));

/* ---------- بوابة النطق (تشكيل تلقائي مصري قبل الصوت) ---------- */
export const TTS_GATE = optional('TTS_GATE', 'true') !== 'false';

/* ---------- مصدر التفسير الموثوق (spa5k/tafsir_api عبر jsDelivr) ---------- */
export const TAFSIR_BASE = optional('TAFSIR_BASE', 'https://cdn.jsdelivr.net/gh/spa5k/tafsir_api@main/tafsir');
export const TAFSIR_PRIMARY = optional('TAFSIR_PRIMARY', 'ar-tafsir-muyassar');   // الميسّر (أساسي)
export const TAFSIR_SECONDARY = optional('TAFSIR_SECONDARY', 'ar-tafseer-al-saddi'); // السعدي (توسّع) — '' لتعطيله

/* ---------- صفحات المصحف (مصحف المدينة — quran.com images) ---------- */
export const MUSHAF_BASE = optional('MUSHAF_BASE', 'https://raw.githubusercontent.com/GovarJabbar/Quran-PNG/master');

/* ---------- قاعدة البيانات (Supabase) ---------- */
export const SUPABASE = {
  url: () => required('SUPABASE_URL'),
  // استخدم مفتاح service_role (مش anon) عشان الكتابة من الباك إند.
  key: () => required('SUPABASE_KEY'),
};

/* ---------- يوتيوب ---------- */
export const YOUTUBE = {
  clientId: () => required('YOUTUBE_CLIENT_ID'),
  clientSecret: () => required('YOUTUBE_CLIENT_SECRET'),
  refreshToken: () => required('YOUTUBE_REFRESH_TOKEN'),
  privacyStatus: optional('YOUTUBE_PRIVACY', 'unlisted') as 'private' | 'public' | 'unlisted',
};

/* ---------- الشورتس (توليد من الكاش + رفع مجدول تلقائي) ---------- */
export const SHORTS = {
  // توليد الشورتس العمودية من كاش الحلقة (صفر API).
  enabled: optional('SHORTS_ENABLED', 'true').toLowerCase() !== 'false',
  // رفعها تلقائيًا على يوتيوب (private + نشر مجدوَل).
  upload: optional('SHORTS_UPLOAD', 'true').toLowerCase() !== 'false',
  // أول شورت ينشر بعد كام يوم من نشر الحلقة.
  firstDelayDays: parseInt(optional('SHORTS_FIRST_DELAY_DAYS', '1'), 10),
  // المسافة بالأيام بين كل شورت والتاني (تنقيط بدل دفعة واحدة).
  intervalDays: parseInt(optional('SHORTS_INTERVAL_DAYS', '2'), 10),
  // ساعة النشر بتوقيت UTC (15 UTC ≈ 6 مساءً بتوقيت الكويت / 5 مساءً مصر).
  publishHourUtc: parseInt(optional('SHORTS_PUBLISH_HOUR_UTC', '15'), 10),
};

/* ---------- مجلدات العمل + أبعاد الفيديو ---------- */
export const VIDEO = { width: 1920, height: 1080, fps: 30 };
export const WORK_ROOT = optional('WORK_DIR', path.join(process.cwd(), 'data', 'renders'));
export const ASSETS_DIR = path.join(process.cwd(), 'assets');
export const LOGO_PATH = path.join(ASSETS_DIR, 'logo.png');
export const OUTRO_PATH = path.join(ASSETS_DIR, 'outro.mp4');
export const INTRO_AUDIO_PATH = path.join(ASSETS_DIR, 'intro.mp3'); // انترو ثابت (يتولّد مرة واحدة)

/* ---------- تأثير رسم الصور (Draw-in) ---------- */
export const DRAW_REVEAL = optional('DRAW_REVEAL', 'true') !== 'false'; // شغّال افتراضيًا
export const REVEAL_SECS = parseFloat(optional('REVEAL_SECS', '2.5'));  // (قديم) لم يعد يحدّد المدة
export const REVEAL_END_BUFFER = parseFloat(optional('REVEAL_END_BUFFER', '2.5')); // الرسم يكتمل قبل نهاية الشرح بكام ثانية
export const REVEAL_PACE = parseFloat(optional('REVEAL_PACE', '0.75'));   // نسبة وقت الشرح المستخدمة للتلوين (أقل = أسرع)
export const DRAW_REAL = optional('DRAW_REAL', 'true') !== 'false'; // رسم حقيقي ضربة-ضربة (تحليل الرسمة في الكود)
export const REVEAL_SKETCH_TR = optional('REVEAL_SKETCH_TR', 'dissolve'); // ظهور الخطوط: نقط بتكبر وتتجمّع
export const REVEAL_COLOR_TR = optional('REVEAL_COLOR_TR', 'wipetl');    // التلوين: حافة حادة من الركن
export const PENCIL_VOLUME = parseFloat(optional('PENCIL_VOLUME', '0.5')); // مستوى صوت القلم (كان 0.35 — هادي زيادة تحت السرد)
export const PENCIL_IMG = path.join(ASSETS_DIR, 'pencil.png'); // صورة القلم (شفافة) — اختياري
export const PENCIL_SND = path.join(ASSETS_DIR, 'pencil.mp3'); // صوت خربشة القلم — اختياري

// خط عربي للترجمة المحروقة (يُركَّب في الـ workflow عبر apt: fonts-noto)
export const ARABIC_FONT = optional('ARABIC_FONT', 'Noto Naskh Arabic');
