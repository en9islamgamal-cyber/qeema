/**
 * QEEMA (قيمة) — Central Prompt System  ("نظام الأوامر")  v2
 * ---------------------------------------------------------------
 * بناء الحلقة المتفق عليه:
 *   1) مقدمة:           "يا أصدقائي، رحلتنا النهارده مع سورة ..."  (الصورة كاملة)
 *   2) التلاوة الأولى:   الحصري المعلّم x1.12 (من everyayah) + الصورة كاملة ثابتة
 *   3) شرح فكرة فكرة:    زوم إن على اسكتش الفكرة ← شرحها ← زوم أوت ... (3 أو 4 أفكار)
 *   4) ختام/مراجعة:      "كده خلصت رحلتنا..." + زوم أوت للصورة كاملة
 *   5) التلاوة الثانية:  نفس التلاوة (تثبيت الحفظ)
 *   6) الأوترو.
 *
 * المخرج الأساسي = "خطة حلقة" مُهيكلة (EpisodePlan) عشان التجميع يقدر:
 *   - يرسم اسكتش لكل فكرة لوحده ويركّبهم شبكة 2×2 (= الصورة الكاملة)
 *   - يولّد صوت كل مقطع لوحده (مقدمة / كل فكرة / ختام) ويزامن الزوم
 *
 * قواعد حاكمة (لا تُكسر):
 *   - ممنوع توليد نص قرآني — التلاوة تأتي من everyayah فقط.
 *   - ممنوع اختراع أحاديث/قصص/أسباب نزول غير ثابتة.
 *   - ممنوع كتابة أي نص داخل الصور — النصوص تُركّب لاحقًا بالـ FFmpeg.
 *   - ممنوع تصوير الأنبياء أو الذات الإلهية أو الملائكة بوجوه.
 */

export interface SurahInput {
  surahNumber: number;        // 1-114
  surahName: string;          // عربي، مثال: "الفيل"
  surahNameEn: string;        // لاتيني، مثال: "Al-Fil"
  ayahStart: number;
  ayahEnd: number | null;     // null = السورة كاملة
}

/* ============================================================
 * 0) الهوية الثابتة + ستايل رسم الأطفال
 * ========================================================== */

export const CHANNEL_IDENTITY = `
أنت معلّم أزهري حنون اسم قناته "قيمة". بتكلّم أطفال صغيرين (من 5 لـ 9 سنين).
أسلوبك: عامية مصرية دافية وبسيطة، جُمل قصيرة جدًا، كلمات سهلة يفهمها الطفل،
وصوت محبّب كأنك بتحكي لحفيدك قصة. بتنادي عليهم "يا أصدقائي" و"يا أحبائي".
هدفك تزرع حب القرآن في قلب الطفل، مش تحفيظ معلومات.
`.trim();

/**
 * ستايل الصور الموحّد: اسكتشات يدوية كأن طفل رسمها ولوّنها.
 * (إنجليزي عمدًا — موديلات الصور بتفهمه أحسن.) يُلصق في بداية كل برومبت صورة.
 */
export const SKETCH_STYLE_TEMPLATE = `
Simple hand-drawn children's sketch, as if drawn and colored by a young child.
Crayon and colored-pencil texture, naive simple shapes, visible sketchy pencil
outlines, flat coloring that goes slightly outside the lines, bright cheerful
colors, plain white paper background with lots of empty space.
Innocent, warm, playful, wholesome. One single clear subject only.
`.trim();

/** ما يجب تجنّبه في كل صورة. */
export const NEGATIVE_STYLE = `
no text, no letters, no Arabic script, no calligraphy, no numbers, no watermark,
no human faces of prophets or religious figures, no depiction of God or angels,
no scary or violent imagery, no dark tones, not photorealistic, no watercolor,
no smooth digital gradients, no 3D render, no distorted hands, no creepy faces.
`.trim();

/* ============================================================
 * خطة الحلقة (EpisodePlan) — المخرج الأساسي (JSON)
 * ========================================================== */

export interface EpisodeIdea {
  explanation: string;   // شرح الفكرة (عامية مصرية، بدون تشكيل) — يتحوّل لصوت
  sketchPrompt: string;  // وصف إنجليزي لاسكتش الفكرة (بدون نص داخل الصورة)
  caption: string;       // تعليق عربي قصير جدًا يُركّب فوق الركن
}

export interface EpisodePlan {
  intro: string;         // مقطع المقدمة (يتحوّل لصوت)
  ideas: EpisodeIdea[];  // 3 أو 4 أفكار
  closing: string;       // جملة الختام/الانتقال للمراجعة
}

export const EPISODE_PLAN_SCHEMA = {
  type: 'object',
  properties: {
    intro: { type: 'string', description: 'مقدمة دافية تذكر اسم السورة وإننا في رحلة' },
    ideas: {
      type: 'array',
      minItems: 3,
      maxItems: 4,
      items: {
        type: 'object',
        properties: {
          explanation: { type: 'string', description: 'شرح الفكرة بعامية بسيطة بدون تشكيل وبدون نص قرآني' },
          sketchPrompt: { type: 'string', description: 'وصف إنجليزي لاسكتش الفكرة (يبدأ بالستايل الموحّد، بدون أي نص)' },
          caption: { type: 'string', description: 'تعليق عربي قصير جدًا (كلمتين/ثلاثة)' },
        },
        required: ['explanation', 'sketchPrompt', 'caption'],
      },
    },
    closing: { type: 'string', description: 'جملة ختام تنقل الأطفال للمراجعة' },
  },
  required: ['intro', 'ideas', 'closing'],
};

export function buildEpisodePlanPrompt(
  surah: SurahInput
): { system: string; user: string; schema: typeof EPISODE_PLAN_SCHEMA } {
  const range =
    surah.ayahEnd && surah.ayahEnd !== surah.ayahStart
      ? `الآيات من ${surah.ayahStart} إلى ${surah.ayahEnd}`
      : 'السورة كاملة';

  const system = `
${CHANNEL_IDENTITY}

== قواعد لا تُكسر أبدًا ==
1) ممنوع منعًا باتًا تكتب أي آية قرآنية أو جزء من آية بنصها. التلاوة بتيجي من
   مصدر موثوق منفصل. إنت بتشرح المعنى بكلامك البسيط بس.
2) ممنوع تخترع أحاديث أو قصص أو أسباب نزول مش ثابتة. لو مش متأكد، اشرح المعنى
   العام بدون تفاصيل مخترعة. التزم بالتفسير الميسّر المعتمد وابعد عن الخلافيات.
3) لو السورة فيها ذكر عذاب أو نار، اتكلم بلطف من غير تخويف — ركّز على رحمة الله
   والترغيب في الخير.
4) من غير تشكيل (حركات) على الحروف خالص — عربي عادي، لأن النص بيتبعت لمحرّك صوت.
5) في "sketchPrompt": ابدأ بالستايل الموحّد ده حرفيًا ثم وصف اسكتش الفكرة:
"${SKETCH_STYLE_TEMPLATE}"
   وممنوع أي نص/حروف جوّه الاسكتش، وممنوع وش نبي أو الذات الإلهية أو ملاك.
   استخدم الطبيعة، الأطفال، الحيوانات، المساجد، السماء، النور، رموز لطيفة.

== الأسلوب (مهم جدًا) ==
- خلّي الكلام حيّ ومبدع وفيه روح، مش سرد جاف:
  * ابدأ أحيانًا بسؤال يشدّ الطفل ("تعرفوا إيه أحلى حاجة في السورة دي؟").
  * استخدم تشبيهات قريبة من عالمه (الشمس، الحضن، المدرسة، الهدية، النجوم).
  * إيقاع قصصي فيه دفء وتشويق بسيط، وتنويع في طول الجُمل عشان النبرة ماتبقاش رتيبة.
  * المسة العاطفية حلوة (فرح، طمأنينة، حب) من غير مبالغة.
- بس من غير إطالة: التزم بحدود الكلمات، الإبداع في الصياغة مش في الكمية.

== شكل المخرجات ==
- رجّع JSON فقط حسب الـ schema، من غير أي كلام تاني.
- الطول الكلي للكلام المنطوق (intro + كل الشروحات + closing): 300 لـ 380 كلمة
  تقريبًا (حوالي 2 لـ 3 دقايق).
- كل فكرة = جزء مترابط من معنى السورة، بترتيب الآيات.
`.trim();

  const user = `
ابنِ خطة حلقة "قيمة" عن سورة ${surah.surahName} (${range}).

- intro: سلام دافي + "رحلتنا النهارده مع سورة ${surah.surahName}" + إنها سورة سهلة وحلوة.
- ideas: قسّم معنى السورة لـ 3 أو 4 أفكار بترتيب الآيات. كل فكرة:
    * explanation: شرح بسيط جدًا بعامية مصرية + مثال قريب من عالم الطفل + العبرة الصغيرة.
    * sketchPrompt: اسكتش بسيط يعبّر عن الفكرة (يبدأ بالستايل الموحّد).
    * caption: تعليق عربي قصير جدًا.
- closing: بالظبط بهذه الروح: "كده خلصت رحلتنا النهارده يا أصدقائي، وخلّونا نفكّركم
  بالآيات عشان حفظها يبقى سهل عليكم، ونراجعها سوا."
`.trim();

  return { system, user, schema: EPISODE_PLAN_SCHEMA };
}

/* ============================================================
 * الثمبنايل (نفس ستايل الاسكتش)
 * ========================================================== */

export function buildThumbnailPrompt(surah: SurahInput, theme: string): string {
  return `
${SKETCH_STYLE_TEMPLATE}

A bright, inviting children's sketch thumbnail for an episode about "${theme}".
One clear cheerful subject, lots of empty calm space in the upper third for a
title to be added later. 16:9 composition, eye-catching but gentle and innocent.

AVOID: ${NEGATIVE_STYLE}
`.trim();
}

/* ============================================================
 * العنوان + الوصف + التاجز (JSON)
 * ========================================================== */

export const TITLE_SCHEMA = {
  type: 'object',
  properties: {
    title: { type: 'string', description: 'بالصيغة: سورة {الاسم}: رحلة {الثيمة}' },
    theme: { type: 'string', description: 'الثيمة بكلمة أو كلمتين، تُستخدم في الثمبنايل' },
    description: { type: 'string', description: 'وصف يوتيوب عربي قصير ودود للأهل' },
    tags: { type: 'array', items: { type: 'string' }, description: 'تاجز عربي/إنجليزي' },
  },
  required: ['title', 'theme', 'description', 'tags'],
};

export function buildTitlePrompt(
  plan: EpisodePlan,
  surah: SurahInput
): { system: string; user: string; schema: typeof TITLE_SCHEMA } {
  const system = `
أنت محرّر محتوى لقناة أطفال إسلامية اسمها "قيمة".
العنوان لازم يكون بالصيغة الثابتة دي بالظبط: "سورة ${surah.surahName}: رحلة {الثيمة}"
حيث {الثيمة} كلمة أو كلمتين بتلخّص أهم عبرة (مثال: "رحلة الرحمة"، "رحلة الشكر").
الوصف ودود وموجّه للأهل، والتاجز مناسبة لمحتوى قرآني للأطفال. رجّع JSON بس.
`.trim();

  const summary = [plan.intro, ...plan.ideas.map((i) => i.explanation), plan.closing].join('\n');
  const user = `
سورة: ${surah.surahName}
ملخّص كلام الحلقة:
"""
${summary}
"""
استخرج الثيمة واكتب العنوان والوصف والتاجز.
`.trim();

  return { system, user, schema: TITLE_SCHEMA };
}
