/**
 * QEEMA — Orchestrator
 * يشغّل البايب لاين كامل لحلقة واحدة داخل نفس الـ run (runner مؤقت على GitHub Actions).
 * loud logging + حالة نهائية مضمونة. الـ batch بيستخدم await صح (إصلاح باج الـ cron القديم).
 */
import * as fs from 'fs';
import * as path from 'path';
import { DB } from './db.ts';
import { WORK_ROOT } from './config.ts';
import { Episode, episodeToSurah } from './types.ts';
import { fetchRecitation, getAyahCount } from './reciter.ts';
import { generateEpisodePlan, generateTitle } from './llm.ts';
import { synthesize } from './voice.ts';
import { generateImage } from './images.ts';
import { buildGrid, assembleEpisode, cellLayout } from './video.ts';
import { uploadVideo } from './youtube.ts';
import { buildThumbnailPrompt } from './prompts.ts';
import { fetchSurahAyat, ayahRangeForTts } from './quran.ts';

export async function runEpisode(idOrNumber: string): Promise<void> {
  const episode = await DB.getEpisodeById(idOrNumber);
  if (!episode) throw new Error(`[orchestrator] الحلقة "${idOrNumber}" غير موجودة في جدول episodes.`);

  const ep: Episode = episode;
  const surah = episodeToSurah(ep);
  const workDir = path.join(WORK_ROOT, ep.id);
  fs.mkdirSync(workDir, { recursive: true });

  await DB.log(ep.id, 'scheduler', 'info', `بدء معالجة الحلقة ${ep.episodeNumber}: سورة ${ep.surahName} (حالة: ${ep.status})`);

  try {
    /* 1) الخطة (LLM) — الـ LLM يحدد أرقام الآيات فقط */
    await DB.setStatus(ep.id, 'scripting');
    await DB.log(ep.id, 'scripting', 'info', 'توليد خطة الحلقة (تفسير + نطاقات آيات + اسكتشات)…');
    const totalAyat = getAyahCount(surah.surahNumber);
    const plan = await generateEpisodePlan(surah, ep.id, totalAyat);
    await DB.savePlan(ep.id, plan);
    const titleInfo = await generateTitle(plan, surah, ep.id);
    await DB.setTitle(ep.id, titleInfo.title);
    await DB.log(ep.id, 'scripting', 'success', `الخطة جاهزة: ${plan.ideas.length} أفكار — العنوان: ${titleInfo.title}`);

    /* نص الآيات بالتشكيل من مصدر موثوق (مش من الـ LLM) */
    await DB.log(ep.id, 'scripting', 'info', 'جلب نص الآيات بالتشكيل من مصدر موثوق…');
    const ayatMap = await fetchSurahAyat(surah.surahNumber);

    /* 2) التلاوة (everyayah) */
    await DB.setStatus(ep.id, 'asset_generation');
    await DB.log(ep.id, 'asset_generation', 'info', 'تنزيل ودمج التلاوة…');
    const recitation = await fetchRecitation(surah, workDir);

    /* 3) الصوت (ElevenLabs) — لكل فكرة: [الآية بصوت الشرح] ثم [الشرح] */
    await DB.log(ep.id, 'asset_generation', 'info', 'توليد التعليق الصوتي (الآية + الشرح)…');
    const introAudio = (await synthesize(plan.intro, path.join(workDir, 'narr_intro.mp3'))).filePath;
    const ideaAudios: string[] = [];
    for (let i = 0; i < plan.ideas.length; i++) {
      const idea = plan.ideas[i];
      const ayahText = ayahRangeForTts(ayatMap, idea.ayahStart, idea.ayahEnd);
      // الآية الأول (بالتشكيل من المصدر) وبعدها الشرح — كله بصوت الشرح
      const segmentText = ayahText ? `${ayahText} ... ${idea.explanation}` : idea.explanation;
      ideaAudios.push((await synthesize(segmentText, path.join(workDir, `narr_idea${i}.mp3`))).filePath);
    }
    const closingAudio = (await synthesize(plan.closing, path.join(workDir, 'narr_closing.mp3'))).filePath;

    /* 4) الصور: اسكتش لكل فكرة + ثمبنايل */
    await DB.log(ep.id, 'asset_generation', 'info', 'توليد الاسكتشات والثمبنايل…');
    const sketchPaths: string[] = [];
    for (let i = 0; i < plan.ideas.length; i++) {
      sketchPaths.push(await generateImage(plan.ideas[i].sketchPrompt, path.join(workDir, `sketch${i}.png`)));
    }
    const thumbnailPath = await generateImage(
      buildThumbnailPrompt(surah, titleInfo.theme),
      path.join(workDir, 'thumbnail.png')
    );
    const gridImage = await buildGrid(sketchPaths, workDir);

    /* 5) التجميع (FFmpeg) — التخطيط يتأقلم مع عدد الأفكار */
    await DB.setStatus(ep.id, 'rendering');
    await DB.log(ep.id, 'rendering', 'info', 'تجميع الفيديو النهائي…');
    const layout = cellLayout(plan.ideas.length);
    const ideasForVideo = plan.ideas.map((idea, i) => ({
      focus: layout[i],            // خلية الفكرة في الشبكة (للزوم)
      audioPath: ideaAudios[i],
      caption: idea.caption,
    }));
    const finalVideo = await assembleEpisode({
      workDir, gridImage,
      recitationPath: recitation.filePath,
      introAudio, closingAudio,
      ideas: ideasForVideo,
      introCaption: titleInfo.title,
    });
    await DB.saveFinalVideoUrl(ep.id, finalVideo);

    /* 6) الرفع (YouTube) */
    await DB.setStatus(ep.id, 'publishing');
    await DB.log(ep.id, 'publishing', 'info', 'رفع الفيديو على يوتيوب…');
    const videoId = await uploadVideo({
      filePath: finalVideo,
      title: titleInfo.title,
      description: `${titleInfo.description}\n\nقناة قيمة — تفسير القرآن للأطفال.\nالثمبنايل: ${path.basename(thumbnailPath)}`,
      tags: titleInfo.tags,
    });

    await DB.setPublished(ep.id, videoId);
    await DB.log(ep.id, 'publishing', 'success', `✅ اكتملت الحلقة ${ep.episodeNumber}. https://youtube.com/watch?v=${videoId}`);
    console.log('[orchestrator] PIPELINE COMPLETE');
  } catch (err: any) {
    const msg = String(err?.message || err);
    console.error(`[orchestrator] PIPELINE FAILED للحلقة ${ep.episodeNumber}: ${msg}`);
    await DB.markFailed(ep.id, msg, (ep.retryCount || 0) + 1);
    await DB.log(ep.id, 'scheduler', 'error', `فشل التشغيل: ${msg}`);
    throw err; // مهم: نخلّي الـ process يخرج بـ exit code != 0
  }
}

/** يختار حلقة فاشلة قابلة لإعادة المحاولة، وإلا أول حلقة planned. يستخدم await صح. */
export async function runNextScheduled(): Promise<void> {
  const episodes = await DB.getEpisodes();
  const resumable = episodes.find((e) => e.status === 'failed' && e.retryCount < 3);
  if (resumable) {
    console.log(`[orchestrator] إعادة محاولة حلقة فاشلة: ${resumable.episodeNumber} (محاولة ${resumable.retryCount})`);
    await runEpisode(resumable.id);   // ← await (الباج القديم كان بيخرج قبل الشغل)
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
