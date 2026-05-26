# 🌙 QEEMA v2 — قناة قِيمة

نظام أوتوماتيكي لإنتاج فيديوهات تعليمية إسلامية للأطفال 6-10 سنين على يوتيوب.
كل فيديو فيه:

- 🎙️ تلاوة قرآنية أصلية بصوت الشيخ محمود خليل الحصري
- 📖 شرح بسيط للآيات باللهجة المصرية، كأنه شيخ أزهري بيتكلم
- 🎨 صور watercolor + ink جميلة من Leonardo.ai
- 🎬 mounting سينمائي بـ Ken Burns motion و crossfades
- 🤲 يبدأ بـ hook جذّاب، ينتهي بدعاء

---

## 🏗️ المعمارية (مبسطة)

```
   ┌─────────────────────────────────────────────┐
   │  Step 1: نص الآية من quran.com (verified)    │
   └──────────────────┬──────────────────────────┘
                      ↓
   ┌─────────────────────────────────────────────┐
   │  Step 2: Gemini call #1 — Sheikh Tafsir       │
   │          → EpisodeNarration                    │
   └──────────────────┬──────────────────────────┘
                      ↓
   ┌─────────────────────────────────────────────┐
   │  Step 3: Gemini call #2 — Hook + Visuals      │
   │          → EpisodeHookAndVisuals               │
   └──────────────────┬──────────────────────────┘
                      ↓
   ┌─────────────────────────────────────────────┐
   │  Step 4: Leonardo — 8-12 صور watercolor       │
   │          ElevenLabs — 5 narration audios       │
   │          everyayah.com — التلاوة الكاملة        │
   └──────────────────┬──────────────────────────┘
                      ↓
   ┌─────────────────────────────────────────────┐
   │  Step 5: FFmpeg — mixing + rendering          │
   │          → final MP4 1920×1080                  │
   └──────────────────┬──────────────────────────┘
                      ↓
   ┌─────────────────────────────────────────────┐
   │  Step 6: YouTube — upload + thumbnail set     │
   └─────────────────────────────────────────────┘
```

**تكلفة كل حلقة:** 2 Gemini calls + 9-13 Leonardo images + 5 ElevenLabs synths
+ N TIlawah downloads (مجاني).

---

## 📐 ترتيب الفيديو النهائي

```
[0:00]  Hook + صورة الـ hook        (~15s)
[0:20]  Intro + intro visual          (~12s)
[0:32]  التلاوة الأولى (كاملة)         (~30-40s)
[1:10]  الشرح المتدفق آية آية         (3-4 دقايق)
        — كل آية مع صورتها
[4:30]  جملة التذكير                  (~10s)
[4:40]  التلاوة الثانية (نفس التلاوة)   (~30-40s)
[5:20]  Outro + دعاء + صورة ختامية    (~15s)
```

**مدة تقريبية:** 5-7 دقايق (مثالي للأطفال 6-10).

---

## 🚀 Setup

### 1. Secrets في GitHub

افتح: Settings → Secrets and variables → Actions → New repository secret

| Secret | الوصف |
|---|---|
| `GEMINI_API_KEY` | مفتاح Gemini أساسي |
| `GEMINI_API_KEY_2` | مفتاح Gemini ثاني (اختياري للـ rotation) |
| `GEMINI_API_KEY_3` | مفتاح Gemini ثالث (اختياري) |
| `ELEVENLABS_API_KEY` | مفتاح ElevenLabs |
| `ELEVENLABS_VOICE_ID` | voice ID — افتراضياً `UR972wNGq3zluze0LoIp` |
| `LEONARDO_API_KEY` | مفتاح Leonardo.ai |
| `SUPABASE_URL` | URL مشروع Supabase |
| `SUPABASE_KEY` | service-role key |
| `YOUTUBE_CLIENT_ID` | OAuth client ID |
| `YOUTUBE_CLIENT_SECRET` | OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | OAuth refresh token |

### 2. **اللوجو** ⚠️ خطوة مهمة جداً

ضع ملف اللوجو الخاص بقناتك في:

```
assets/logo.png
```

**المواصفات:**
- صيغة PNG مع شفافية (transparency)
- مربع (مثلاً 512×512)
- الـ pipeline هـ-يـ-resize-ـه تلقائياً لكل الاستخدامات:
  - 420px للـ intro splash (أول 2 ثانية)
  - 180px كـ watermark دائم في كل الفيديو
  - 240px في الـ thumbnails

**إذا لم تضع لوجو:** الفيديو يكتمل بدون watermark وبدون intro splash.

### 3. تشغيل أول حلقة

في GitHub: Actions → "QEEMA v2 — Episode Pipeline" → Run workflow

- **episode_number**: 3 (مثلاً، سورة الفلق)
- **dry_run**: ✅ (للتجربة بدون رفع على يوتيوب)
- **force**: ⬜ (اتركه فاضي أول مرة)

تابع الـ logs. الفيديو الناتج هـ-يتـ-uploaded كـ artifact في آخر الـ run.

### 4. التشغيل من اللوكال (اختياري)

```bash
# Install
pip install -r requirements.txt
sudo apt install ffmpeg fonts-noto-core   # على Ubuntu

# Set env vars (أو ضعهم في .env واستخدم python-dotenv)
export GEMINI_API_KEY=...
export ELEVENLABS_API_KEY=...
# ... باقي الـ keys

# Run
python main.py --episode 3 --dry-run
```

---

## 📋 Curriculum (38 حلقة)

| # | السورة | الآيات | الموضوع |
|---|---|---|---|
| 1 | الفاتحة | 1-7 | أم الكتاب |
| 2 | الناس | 1-6 | الاستعاذة من شر الناس |
| 3 | الفلق | 1-5 | الاستعاذة من شر الخلق |
| 4 | الإخلاص | 1-4 | صفات الله الواحد |
| 5 | المسد | 1-5 | عاقبة الكفر بالحق |
| ... | ... | ... | ... |
| 38 | النبأ | 1-40 | النبأ العظيم |

شوف `data/curriculum.py` للقائمة الكاملة.

**لإضافة حلقة جديدة:** عدّل `EPISODES` dict في `data/curriculum.py`.

---

## 🛡️ ضوابط عقدية مدمجة

الـ Sheikh prompt فيه قواعد عقدية صارمة:

- ❌ ممنوع تشبيه أي مفهوم روحي بأي عملية تقنية أو ميكانيكية
  - مثال: الاستعاذة ≠ إشارة موبايل
  - الدعاء ≠ رسالة WhatsApp
  - السحر ≠ فيروس كمبيوتر
- ❌ ممنوع رسم وجه نبي أو الذات الإلهية
- ❌ ممنوع تكنولوجيا حديثة في الصور
- ❌ ممنوع نص داخل الصور
- ✅ التشبيهات مسموحة من: الطبيعة، الأسرة، الأمان البسيط

---

## 🔍 Debugging

### الفيديو ما اتولّدش؟

شوف الـ logs في GitHub Actions، خصوصاً:

1. **Step "Verify assets present"** — هل اللوجو موجود؟
2. **Step "Run pipeline"** — ايه آخر سطر قبل الفشل؟

artifacts بيتـ-saved لـ 7 أيام حتى لما الـ run يفشل (state/, logs/, temp/).

### الصوت متقطع؟

شوف `temp/audio_mix/` — كل segment موجود لوحده، ممكن تشغّلها عشان تعرف الـ
segment اللي فيه المشكلة.

### Gemini schema validation فشلت؟

يـ-printed في الـ log كلمة `Schema validation failed`. شوف الـ output،
ممكن Gemini رجّع field ناقص. الـ system retries تلقائياً مرة على نفس الـ key.

---

## 🌳 هيكل الـ Repo

```
qeema-v2/
├── .github/workflows/
│   └── pipeline.yml             # GitHub Actions workflow
├── core/
│   ├── config.py                # كل الـ settings + env vars
│   └── models.py                # Pydantic schemas
├── pipeline/
│   ├── prompts.py               # الـ 2 prompts الثابتة
│   ├── tafsir_generator.py      # Gemini call #1
│   ├── hook_visuals_generator.py # Gemini call #2
│   └── orchestrator.py          # يربط الكل
├── assets_engines/
│   ├── gemini_client.py         # Gemini wrapper + key rotation
│   ├── ayah_text_fetcher.py     # quran.com API
│   ├── tilawah_fetcher.py       # everyayah.com (الحصري)
│   ├── leonardo_client.py       # Leonardo.ai
│   └── elevenlabs_client.py     # ElevenLabs
├── video/
│   ├── audio_director.py        # mixing الصوت
│   ├── video_assembler.py       # rendering MP4
│   └── thumbnail_builder.py     # YouTube thumbnails
├── publishing/
│   └── youtube_uploader.py      # OAuth + upload
├── data/
│   └── curriculum.py            # 38 episode definitions
├── assets/
│   └── logo.png                 # ⚠️ ضع لوجوك هنا
├── main.py                      # entry point
├── requirements.txt
├── README.md
└── SETUP.md                     # تفاصيل إضافية
```

---

## 📜 License

Proprietary. All rights reserved by the QEEMA / VALUE team.
