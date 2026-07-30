/**
 * QEEMA — Orchestrator
 * يشغّل البايب لاين كامل لحلقة واحدة داخل نفس الـ run (runner مؤقت على GitHub Actions).
 * loud logging + حالة نهائية مضمونة. الـ batch بيستخدم await صح (إصلاح باج الـ cron القديم).
 */
import * as fs from 'fs';
import * as path from 'path';
import { DB } from './db.ts';
import { WORK_ROOT, SHORTS, ASSETS_DIR, LOGO_PATH } from './config.ts';
import { Episode, episodeToSurah } from './types.ts';
import { fetchRecitation, fetchAyahClip, getAyahCount } from './reciter.ts';
import { generateEpisodePlan, generateTitle } from './llm.ts';
import { synthesize, concatAudio } from './voice.ts';
import { generateImage } from './images.ts';
import { buildGrid, assembleEpisode, cellLayout, renderThumbnailText, compressThumbnail, ffprobeDuration } from './video.ts';
import { generateShorts } from './shorts.ts';
import { uploadVideo } from './youtube.ts';
import { buildThumbnailPrompt } from './prompts.ts';
import { fetchSurahAyat, ayahRangeForTts } from './quran.ts';

/** تحويل الأرقام لعربية-هندية (١٢٣) للثمبنايل. */
const toArabicDigits = (n: number): string =>
  String(n).replace(/[0-9]/g, (d) => '٠١٢٣٤٥٦٧٨٩'[Number(d)]);

/** وضع الاختبار: TEST_MODE=true (أو 1) بيشغّل الصوت كامل بدون استهلاك credits صور. */
const isTestMode = (): boolean => {
  const v = (process.env.TEST_MODE || '').toLowerCase();
  return v === 'true' || v === '1' || v === 'yes';
};

/** بيختار صورة ثابتة من assets للاستخدام في وضع الاختبار (بدل توليد بالـ AI). */
function pickTestStill(): string {
  const candidates = [
    path.join(ASSETS_DIR, 'test.png'),
    path.join(ASSETS_DIR, 'thumbnail.png'),
    LOGO_PATH,
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  throw new Error('[orchestrator] وضع الاختبار محتاج صورة ثابتة في assets/ (test.png أو thumbnail.png أو logo.png).');
}

export async function runEpisode(idOrNumber: string): Promise<void> {
  const episode = await DB.getEpisodeById(idOrNumber);
  if (!episode) throw new Error(`[orchestrator] الحلقة "${idOrNumber}" غير موجودة في جدول episodes.`);

  const ep: Episode = episode;
  const surah = episodeToSurah(ep);
  const workDir = path.join(WORK_ROOT, ep.id);
  fs.mkdirSync(workDir, { recursive: true });

  const TEST = isTestMode();
  const testStill = TEST ? pickTestStill() : '';
  if (TEST) {
    await DB.log(ep.id, 'scheduler', 'info', `🧪 وضع الاختبار مُفعّل: صوت كامل + صور ثابتة من assets (${path.basename(testStill)}) + بدون شورتس + رفع unlisted.`);
  }

  await DB.log(ep.id, 'scheduler', 'info', `بدء معالجة الحلقة ${ep.episodeNumber}: سورة ${ep.surahName} (حالة: ${ep.status})`);

  try {
    /* 1) الخطة (LLM) — الـ LLM يحدد أرقام الآيات فقط */
    await DB.setStatus(ep.id, 'scripting');
    await DB.log(ep.id, 'scripting', 'info', 'توليد خطة الحلقة (تفسير + نطاقات آيات + اسكتشات)…');
    const totalAyat = getAyahCount(surah.surahNumber);
    const plan = await generateEpisodePlan(surah, ep.id, totalAyat);

    // ضمان ترتيب الآيات تصاعديًا (يمنع لخبطة الترتيب مهما رجّع الموديل) + تحذير على الفجوات/التداخل
    plan.ideas.sort((a: any, b: any) => (a.ayahStart - b.ayahStart) || (a.ayahEnd - b.ayahEnd));
    for (let i = 1; i < plan.ideas.length; i++) {
      const prev: any = plan.ideas[i - 1], cur: any = plan.ideas[i];
      if (cur.ayahStart <= prev.ayahEnd) {
        await DB.log(ep.id, 'scripting', 'warn', `⚠️ تداخل في ترتيب الآيات: فكرة تنتهي عند ${prev.ayahEnd} والتالية تبدأ من ${cur.ayahStart}.`);
      } else if (cur.ayahStart > prev.ayahEnd + 1) {
        await DB.log(ep.id, 'scripting', 'warn', `⚠️ فجوة في تغطية الآيات: من ${prev.ayahEnd + 1} لـ ${cur.ayahStart - 1} مش مغطّاة.`);
      }
    }

    await DB.savePlan(ep.id, plan);
    const titleInfo = await generateTitle(plan, surah, ep.id);
    await DB.setTitle(ep.id, titleInfo.title);
    await DB.log(ep.id, 'scripting', 'success', `الخطة جاهزة: ${plan.ideas.length} أفكار — العنوان: ${titleInfo.title}`);

    /* نص الآيات بالتشكيل من مصدر موثوق (مش من الـ LLM) */
    await DB.log(ep.id, 'scripting', 'info', 'جلب نص الآيات بالتشكيل من مصدر موثوق…');
    const ayatMap = await fetchSurahAyat(surah.surahNumber);

    /* 2) التلاوة (everyayah) */
    await DB.setStatus(ep.id, 'asset_generation');
    await DB.log(ep.id, 'asset_generation', 'info', 'تنزيل ودمج التلاوة المجمعة في البداية…');
    const recitation = await fetchRecitation(surah, workDir);

    /* 3) الصوت (ElevenLabs للشرح + الحصري للآيات المفصلة) */
    await DB.log(ep.id, 'asset_generation', 'info', 'توليد التعليق الصوتي وتقسيم التلاوة قبل كل شرح…');
    const introAudio = (await synthesize(plan.intro, path.join(workDir, 'narr_intro_var.mp3'), { tempo: 1.0 })).filePath;

    const ideaAudios: string[] = [];
    const ideaRecStart: number[] = []; // مدة التلاوة المقطّعة في أول كل مقطع (الرسم يبدأ بعدها)
    for (let i = 0; i < plan.ideas.length; i++) {
      const idea = plan.ideas[i];
      const explAudio = (await synthesize(idea.explanation, path.join(workDir, `narr_idea${i}_expl.mp3`), { tempo: 0.92 })).filePath;
      
      try {
        // جلب تلاوة الآيات المحددة للفكرة دي عشان تتشرح
        const ayahClip = await fetchAyahClip(surah.surahNumber, idea.ayahStart, workDir, idea.ayahEnd);
        const ayahDur = await ffprobeDuration(ayahClip.filePath);
        ideaRecStart.push(ayahDur > 0 ? ayahDur : 0);
        ideaAudios.push(await concatAudio([ayahClip.filePath, explAudio], path.join(workDir, `narr_idea${i}.mp3`)));
      } catch (err: any) {
        await DB.log(ep.id, 'asset_generation', 'warn',
          `تعذّر جلب تلاوة آية الفكرة ${i + 1} (${idea.ayahStart}-${idea.ayahEnd}): ${String(err?.message || err)} — هنكمّل بالشرح بس.`);
        ideaRecStart.push(0); // مفيش تلاوة → الرسم من بداية المقطع
        ideaAudios.push(explAudio);
      }
    }
    const closingAudio = (await synthesize(plan.closing, path.join(workDir, 'narr_closing.mp3'), { tempo: 1.0 })).filePath;

    // فاصل منطوق بين التلاوة المتواصلة وبداية التلاوة المقطّعة مع الشرح
    const bridgeText = `يلا بينا يا أصحابي نسمع آيات ${surah.surahName} ونفهمها مع بعض، آية آية.`;
    const bridgeAudio = (await synthesize(bridgeText, path.join(workDir, 'narr_bridge.mp3'), { tempo: 1.0 })).filePath;

    /* 4) الصور: اسكتش لكل فكرة + ثمبنايل بالنص الديناميكي */
    await DB.log(ep.id, 'asset_generation', 'info', 'توليد الاسكتشات والثمبنايل…');
    const sketchPaths: string[] = [];
    for (let i = 0; i < plan.ideas.length; i++) {
      const sp = path.join(workDir, `sketch${i}.png`);
      if (TEST) {
        fs.copyFileSync(testStill, sp); // وضع الاختبار: صورة ثابتة بدل توليد بالـ AI (صفر credits)
      } else {
        await generateImage(plan.ideas[i].sketchPrompt, sp);
      }
      sketchPaths.push(sp);
    }
    
    // إعداد نص الثمبنايل بناءً على طلبك
    const totalAyatForThumb = getAyahCount(surah.surahNumber);
    const tStart = surah.ayahStart || 1;
    const tEnd = surah.ayahEnd && surah.ayahEnd > 0 ? surah.ayahEnd : totalAyatForThumb;
    const isFullSurah = tStart <= 1 && tEnd >= totalAyatForThumb;
    
    const thumbLines = isFullSurah
      ? [`رحلة جديدة في معاني سورة ${surah.surahName}`]
      : [`سورة ${surah.surahName}`, `(الآيات من ${toArabicDigits(tStart)} إلى ${toArabicDigits(tEnd)})`];

    let thumbnailPath = await renderThumbnailText(thumbLines, workDir);
    if (!thumbnailPath) {
      if (TEST) {
        thumbnailPath = path.join(workDir, 'thumbnail.png');
        fs.copyFileSync(testStill, thumbnailPath); // وضع الاختبار: صورة الثمبنايل من الاسيتس بدون AI
        await DB.log(ep.id, 'asset_generation', 'info', `وضع الاختبار: ثمبنايل من الاسيتس (${path.basename(testStill)}).`);
      } else {
        await DB.log(ep.id, 'asset_generation', 'warn', 'مفيش assets/thumbnail.png — هيتولّد ثمبنايل بالـ AI كاحتياطي.');
        thumbnailPath = await generateImage(
          buildThumbnailPrompt(surah, titleInfo.theme),
          path.join(workDir, 'thumbnail.png')
        );
      }
    }
    // ضغط الثمبنايل لـ JPEG 1280×720 (<2MB) عشان رفع يوتيوب ما يفشلش بسبب الحجم
    thumbnailPath = await compressThumbnail(thumbnailPath, workDir);
    const gridImage = await buildGrid(sketchPaths, workDir);

    /* 5) التجميع (FFmpeg) */
    await DB.setStatus(ep.id, 'rendering');
    await DB.log(ep.id, 'rendering', 'info', 'تجميع الفيديو النهائي…');
    const layout = cellLayout(plan.ideas.length);
    const ideasForVideo = plan.ideas.map((idea, i) => ({
      focus: layout[i],
      audioPath: ideaAudios[i],
      caption: idea.caption,
      sketch: sketchPaths[i],
      revealStart: ideaRecStart[i],
    }));
    const finalVideo = await assembleEpisode({
      workDir, gridImage,
      recitationPath: recitation.filePath,
      introAudio, closingAudio, bridgeAudio,
      ideas: ideasForVideo,
      introCaption: titleInfo.title,
    });
    if (!TEST) await DB.saveFinalVideoUrl(ep.id, finalVideo);

    /* 5.5) توليد الشورتس */
    let shorts: string[] = [];
    if (!TEST && SHORTS.enabled) {
      await DB.log(ep.id, 'rendering', 'info', 'توليد الشورتس العمودية من الكاش…');
      shorts = await generateShorts(
        plan.ideas.map((idea, i) => ({
          sketchPath: sketchPaths[i],
          audioPath: ideaAudios[i],
          ayahText: ayahRangeForTts(ayatMap, idea.ayahStart, idea.ayahEnd),
          surahName: surah.surahName,
          ayahStart: idea.ayahStart,
          ayahEnd: idea.ayahEnd,
        })),
        workDir
      );
      await DB.log(ep.id, 'rendering', 'success', `تم توليد ${shorts.length} شورت بصفر credits.`);
    }

    /* 6) الرفع (YouTube) */
    await DB.setStatus(ep.id, 'publishing');
    await DB.log(ep.id, 'publishing', 'info', TEST ? '🧪 وضع الاختبار: رفع نسخة unlisted للمراجعة…' : 'رفع الفيديو على يوتيوب…');
    const videoId = await uploadVideo({
      filePath: finalVideo,
      title: TEST ? `[TEST] ${titleInfo.title}` : titleInfo.title,
      description: `${titleInfo.description}\n\nقناة قيمة — تفسير القرآن للأطفال.`,
      tags: titleInfo.tags,
      thumbnailPath,
    });

    if (TEST) {
      await DB.setStatus(ep.id, ep.status); // رجّع الحالة الأصلية — الاختبار مايغيّرش حالة الحلقة
      await DB.log(ep.id, 'publishing', 'success', `🧪 وضع الاختبار خلص — نسخة مراجعة (مش منشورة رسميًا، ومفيش تغيير في حالة الحلقة): https://youtube.com/watch?v=${videoId}`);
      console.log('[orchestrator] TEST PIPELINE COMPLETE');
      return;
    }

    await DB.setPublished(ep.id, videoId);
    await DB.log(ep.id, 'publishing', 'success', `✅ اكتملت الحلقة ${ep.episodeNumber}. https://youtube.com/watch?v=${videoId}`);

    /* 6.5) رفع الشورتس مجدوَلًا */
    if (SHORTS.enabled && SHORTS.upload && shorts.length) {
      await DB.log(ep.id, 'publishing', 'info', `جدولة رفع ${shorts.length} شورت (كل ${SHORTS.intervalDays} يوم)…`);
      for (let i = 0; i < shorts.length; i++) {
        try {
          const idea = plan.ideas[i];
          const label =
            idea.ayahEnd && idea.ayahEnd !== idea.ayahStart
              ? `الآيات ${idea.ayahStart}-${idea.ayahEnd}`
              : `الآية ${idea.ayahStart}`;
          const when = new Date();
          when.setUTCDate(when.getUTCDate() + SHORTS.firstDelayDays + i * SHORTS.intervalDays);
          when.setUTCHours(SHORTS.publishHourUtc, 0, 0, 0);
          const publishAt = when.toISOString();

          const sId = await uploadVideo({
            filePath: shorts[i],
            title: `سورة ${surah.surahName} — ${label} 🌙 #Shorts`.slice(0, 100),
            description:
              `من قناة قيمة — نخلّي الأطفال يفهموا القرآن بحب 🌙\n` +
              `سورة ${surah.surahName} (${label}).\n\n` +
              `▶️ الحلقة كاملة: https://youtube.com/watch?v=${videoId}\n\n` +
              `#قيمة #قرآن_للأطفال #تفسير #Shorts`,
            tags: [...(titleInfo.tags || []), 'shorts', 'قيمة', 'قرآن للأطفال', 'تفسير للأطفال'].slice(0, 30),
            publishAt,
          });
          await DB.log(ep.id, 'publishing', 'success',
            `📅 شورت ${i + 1}/${shorts.length} مجدوَل للنشر ${publishAt} — https://youtube.com/watch?v=${sId}`);
        } catch (err: any) {
          await DB.log(ep.id, 'publishing', 'warn',
            `فشل رفع/جدولة الشورت ${i + 1}/${shorts.length}: ${String(err?.message || err)} — بنكمّل.`);
        }
      }
    }

    console.log('[orchestrator] PIPELINE COMPLETE');
  } catch (err: any) {
    const msg = String(err?.message || err);
    console.error(`[orchestrator] PIPELINE FAILED للحلقة ${ep.episodeNumber}: ${msg}`);
    if (TEST) {
      try { await DB.setStatus(ep.id, ep.status); } catch {} // رجّع الحالة — متعلّمش الحلقة كـ failed بسبب اختبار
      await DB.log(ep.id, 'scheduler', 'error', `🧪 فشل تشغيل الاختبار (الحالة اترجّعت زي ما كانت): ${msg}`);
    } else {
      await DB.markFailed(ep.id, msg, (ep.retryCount || 0) + 1);
      await DB.log(ep.id, 'scheduler', 'error', `فشل التشغيل: ${msg}`);
    }
    throw err;
  }
}

export async function runNextScheduled(): Promise<void> {
  const episodes = await DB.getEpisodes();
  const resumable = episodes.find((e) => e.status === 'failed' && e.retryCount < 3);
  if (resumable) {
    console.log(`[orchestrator] إعادة محاولة حلقة فاشلة: ${resumable.episodeNumber} (محاولة ${resumable.retryCount})`);
    await runEpisode(resumable.id);
    return;
  }
  const next = episodes.find((e) => e.status === 'planned');
  if (next) {
    console.log(`[orchestrator] تشغيل الحلقة التالية: ${next.episodeNumber} — سورة ${next.surahName}`);
    await runEpisode(next.id);
    return;
  }
  console.log('[orchestrator] مفيش حلقات planned أو فاشلة قابلة لإعادة المحاولة. لا شيء لعمله.');
}
