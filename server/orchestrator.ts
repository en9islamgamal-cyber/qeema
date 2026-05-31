/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { DB } from './db.ts';
import { GeminiService, Type } from './gemini.ts';
import { VideoAssemblyEngine } from './video-assembly.ts';
import { YouTubeService } from './youtube.ts';
import { Episode, VisualSegment } from '../src/types.ts';
import * as path from 'path';

export class PipelineOrchestrator {
  private static activeProcessingId: string | null = null;
  private static isProcessingActive = false;
  private static currentStepLabel = 'idling';
  private static progressAmount = 0;

  static getActiveState() {
    return {
      activeEpisodeId: this.activeProcessingId,
      isProcessing: this.isProcessingActive,
      currentStep: this.currentStepLabel,
      progressPercent: this.progressAmount,
    };
  }

  /**
   * Main entrypoint triggered by either human dispatcher or autonomous schedule event loops.
   * Leverages checkpoint metadata to resume directly from the failed or initial state.
   */
  static async runEpisode(inputEpisodeId: string): Promise<void> {
    if (this.isProcessingActive) {
      throw new Error(`Pipeline active: Episode ${this.activeProcessingId} is currently holding the render thread.`);
    }

    const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(inputEpisodeId);
    const parsedInt = parseInt(inputEpisodeId, 10);
    const isNumeric = !isNaN(parsedInt) && String(parsedInt) === String(inputEpisodeId).trim();
    const fieldQueried = isUuid ? 'id' : (isNumeric ? 'episode_number' : 'id');
    const valueQueried = isUuid ? inputEpisodeId : (isNumeric ? parsedInt : inputEpisodeId);

    const episode = await DB.getEpisodeById(inputEpisodeId);
    if (!episode) {
      throw new Error(`Episode not found. Queried table=episodes, field=${fieldQueried}, value=${valueQueried}`);
    }

    const episodeId = episode.id;
    this.activeProcessingId = episodeId;
    this.isProcessingActive = true;
    this.progressAmount = 10;

    try {
      await DB.log(episodeId, 'scheduler', 'info', `Acquired active processor lock. Status: ${episode.status}`);

      // 0. SELF-HEALING ASSET VERIFICATION (For fresh container instances / CI runners)
      // Check if we are in a late rendering/publishing state but previous assets are missing locally.
      if (episode.status === 'rendering' || episode.status === 'publishing') {
        const expectedVideoFile = path.join(process.cwd(), 'data', 'renders', `${episodeId}_final.mp4`);
        const fs = await import('fs');
        if (!fs.existsSync(expectedVideoFile)) {
          await DB.log(episodeId, 'scheduler', 'warn', `Expected local video file missing at ${expectedVideoFile}. Forcing pipeline state reset to "planned" for fresh regeneration.`);
          episode.status = 'planned'; // This mutates the local memory copy so current run executes correctly!
          await DB.updateEpisode(episodeId, { status: 'planned' });
        }
      }

      // 1. STAGE: scripting
      if (episode.status === 'planned' || episode.status === 'scripting') {
        await DB.updateEpisode(episodeId, { status: 'scripting' });
        this.currentStepLabel = 'Writing Production Narrative...';
        this.progressAmount = 20;

        await DB.log(episodeId, 'scripting', 'info', `Executing script brainstorming for topic: "${episode.topic}"`);
        
        const scriptPrompt = `Write a compelling, professional, and educational narrations script for a YouTube video about: "${episode.topic}". Keep it engaging, straightforward and within 100-200 English words. Output ONLY the raw spoken narrative text. No meta headers, stage notes, bracket labels or tags.`;
        const script = await GeminiService.generateText(scriptPrompt, 'You are an elite, highly professional tech YouTube video scripting director.', episodeId);
        
        await DB.updateEpisode(episodeId, { script, status: 'asset_generation' });
        await DB.log(episodeId, 'scripting', 'success', 'Narration script has been generated and cached securely.');
        
        // Reload episode variables for consistent checkpoints
        episode.script = script;
        episode.status = 'asset_generation';
      }

      // 2. STAGE: asset_generation
      if (episode.status === 'asset_generation') {
        this.currentStepLabel = 'Structuring visual storyboard briefs...';
        this.progressAmount = 35;
        await DB.log(episodeId, 'asset_generation', 'info', 'Mapping narrator script to sequential multi-scene storyboard intervals using Gemini JSON schema.');

        // Format visual storyboards using the recommended Type definitions
        const storyboardSchema = {
          type: Type.ARRAY,
          description: 'A list of highly illustrative scenes matched with narrative segments.',
          items: {
            type: Type.OBJECT,
            properties: {
              prompt: { type: Type.STRING, description: 'Descriptive, visual illustration prompt suited for image generation pipelines' },
              narrativeChunk: { type: Type.STRING, description: 'Subsegment sentence verbatim matching narration script.' },
              duration: { type: Type.INTEGER, description: 'Duration of the visual frame clip segment in seconds.' },
            },
            required: ['prompt', 'narrativeChunk', 'duration'],
          },
        };

        const plannerSystemInstruction = 'You are an experienced YouTube storyboarding and director interface that formats outputs strictly matching JSON schema.';
        const plannerPrompt = `Given this script narrative: "${episode.script}". Analyze the contents and break them down into 3-5 visual sequential storyboard segments. Formulate descriptive image prompts for each.`;

        const storyboardResponse = await GeminiService.generateStructuredJson(
          plannerPrompt,
          storyboardSchema,
          plannerSystemInstruction,
          episodeId
        );

        const visualBriefsWithoutUrls = Array.isArray(storyboardResponse) ? storyboardResponse : [];
        await DB.log(episodeId, 'asset_generation', 'info', 'Compiled storyboard schema structure. Querying Gemini Image models for illustrating actual production assets...');
        
        const visualBriefs = await VideoAssemblyEngine.generateVisualAssets(episodeId, visualBriefsWithoutUrls);
        
        await DB.updateEpisode(episodeId, { visualBriefs, status: 'rendering' });
        await DB.log(episodeId, 'asset_generation', 'success', 'All visual frames and asset files have been illustrated and cached locally.');
        
        // Reload episode variables for consistent checkpoints
        episode.visualBriefs = visualBriefs;
        episode.status = 'rendering';
      }

      // 3. STAGE: rendering
      if (episode.status === 'rendering') {
        this.currentStepLabel = 'Assembling and synthesizing video output...';
        this.progressAmount = 60;
        await DB.log(episodeId, 'rendering', 'info', `Executing text-to-speech audio loop for voice config: ${episode.voiceName || 'Kore'}`);

        const narrationUrl = await VideoAssemblyEngine.synthesizeNarration(episodeId, episode.script || '', episode.voiceName);
        episode.narrationAudioUrl = narrationUrl;

        await DB.log(episodeId, 'rendering', 'info', 'Finished compiling vocals. Blending branding watermarks and creating final mp4 wrappers...');
        const finalVideoUrl = await VideoAssemblyEngine.compileFinalVideo(episode);

        await DB.updateEpisode(episodeId, {
          narrationAudioUrl: narrationUrl,
          finalVideoUrl: finalVideoUrl,
          status: 'publishing',
        });
        await DB.log(episodeId, 'rendering', 'success', `Video render pipeline completed successfully! Saved to playable path: ${finalVideoUrl}`);

        episode.finalVideoUrl = finalVideoUrl;
        episode.status = 'publishing';
      }

      // 4. STAGE: publishing
      if (episode.status === 'publishing') {
        this.currentStepLabel = 'Pushing final video to official unlisted YouTube channel...';
        this.progressAmount = 85;
        await DB.log(episodeId, 'publishing', 'info', 'Pushing final video to official unlisted YouTube channel queue.');

        const filePath = path.join(process.cwd(), 'data', 'renders', `${episodeId}_final.mp4`);
        const responseYt = await YouTubeService.uploadVideo({
          episodeId,
          filePath,
          title: episode.title || 'Qeema YouTube Platform Output',
          description: `Educational production about: ${episode.topic}.\n\nAutomatically designed and engineered by Qeema Autonomous Agent pipelines.`,
          tags: ['Islamic', 'Qeema', 'AI', 'Automation', 'Islamic Video Generator'],
          privacyStatus: 'unlisted', // default to unlisted queue for safety reviews
        });

        await DB.updateEpisode(episodeId, {
          youtubeId: responseYt.videoId,
          youtubePublishDate: new Date().toISOString(),
          status: 'completed',
        });

        this.progressAmount = 100;
        this.currentStepLabel = 'completed';
        await DB.log(episodeId, 'publishing', 'success', `Fully finalized YouTube Automation Pipeline run for target ID ${episodeId}! Finished Stage.`);
      }

    } catch (error: any) {
      const errMsg = error?.message || String(error);
      console.error(`[PIPELINE][CRITICAL-FAILURE] Episode ${episodeId} crashed:`, error);
      
      const updatedEpisode = await DB.getEpisodeById(episodeId);
      const newRetries = (updatedEpisode?.retryCount || 0) + 1;

      await DB.updateEpisode(episodeId, {
        status: 'failed',
        errorLog: errMsg,
        retryCount: newRetries,
      });

      this.currentStepLabel = 'failed';
    } finally {
      this.isProcessingActive = false;
      this.activeProcessingId = null;
    }
  }

  /**
   * Batch execution checker periodically polled on schedule triggers.
   */
  static async triggerPeriodicBatchCron(): Promise<void> {
    console.log('[PIPELINE][CRON] Triggered periodic batch cron checking for pending/failed schedules.');
    const episodes = await DB.getEpisodes();
    const activeRunning = episodes.find((e) => e.status !== 'completed' && e.status !== 'failed' && e.status !== 'planned');
    
    if (activeRunning) {
      console.log(`[PIPELINE][CRON] Scheduled runner bypassed. Active job ID ${activeRunning.id} is currently running at status: "${activeRunning.status}".`);
      return;
    }

    // Process the highest priority failed episode (with under 3 retries) or next pending planned episode
    const candidates = episodes.filter((e) => {
      if (e.status === 'planned') return true;
      if (e.status === 'failed' && (e.retryCount || 0) < 3) return true;
      return false;
    });

    if (candidates.length === 0) {
      console.log('[PIPELINE][CRON] No executable episodes require scheduling at this interval.');
      return;
    }

    // Sort by status failed (retry first) then planned
    candidates.sort((a, b) => {
      if (a.status === 'failed' && b.status !== 'failed') return -1;
      if (a.status !== 'failed' && b.status === 'failed') return 1;
      return 0;
    });

    const targetJob = candidates[0];
    console.log(`[PIPELINE][CRON] Selected target Job ID ${targetJob.id} (Status: ${targetJob.status}, Retries: ${targetJob.retryCount || 0}) for immediate pipeline batch run.`);
    
    await this.runEpisode(targetJob.id);
  }
}