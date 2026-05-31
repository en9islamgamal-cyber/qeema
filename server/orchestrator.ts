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
   * Triggers the continuous pipeline run for a specific episode.
   * Leverages checkpoint metadata to resume directly from the failed or initial state.
   */
  static async runEpisode(episodeId: string): Promise<void> {
    if (this.isProcessingActive) {
      throw new Error(`Pipeline active: Episode ${this.activeProcessingId} is currently holding the render thread.`);
    }

    const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(episodeId);
    const parsedInt = parseInt(episodeId, 10);
    const isNumeric = !isNaN(parsedInt) && String(parsedInt) === String(episodeId).trim();
    const fieldQueried = isUuid ? 'id' : (isNumeric ? 'episode_number' : 'id');
    const valueQueried = isUuid ? episodeId : (isNumeric ? parsedInt : episodeId);

    const episode = await DB.getEpisodeById(episodeId);
    if (!episode) {
      throw new Error(`Episode not found. Queried table=episodes, field=${fieldQueried}, value=${valueQueried}`);
    }

    this.activeProcessingId = episodeId;
    this.isProcessingActive = true;
    this.progressAmount = 10;

    try {
      await DB.log(episodeId, 'scheduler', 'info', `Acquired active processor lock. Status: ${episode.status}`);

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
              timestamp: { type: Type.STRING, description: 'Format 0:00' },
              duration: { type: Type.NUMBER, description: 'Seconds to show this slide frame' },
              prompt: { type: Type.STRING, description: 'Highly descriptive prompt to generate custom beautiful illustrations for the narrator slide.' },
              caption: { type: Type.STRING, description: 'Title or textual caption overlays' },
            },
            required: ['timestamp', 'duration', 'prompt', 'caption'],
          },
        };

        const listPrompt = `Review this video script: "${episode.script || ''}". Segment this script into a logical timeline sequence composed of 2 to 4 visual frames. For every frame, write a distinct timestamp, duration in seconds, descriptive prompt to generate custom beautiful illustration frames, and an elegant caption overlay.`;
        const visualBriefsRaw = await GeminiService.generateJSON<VisualSegment[]>(
          listPrompt,
          storyboardSchema,
          'You are a senior Hollywood digital visual effects and storyboard director.',
          episodeId
        );

        // Populate and save visual frames array to segment
        await DB.updateEpisode(episodeId, { visualBriefs: visualBriefsRaw });
        
        this.currentStepLabel = 'Synthesizing original frame slides...';
        this.progressAmount = 50;
        
        // Generate real visual images using Gemini Image generator
        const readyVisualSegments = await VideoAssemblyEngine.generateVisualAssets(episodeId, visualBriefsRaw);
        
        await DB.updateEpisode(episodeId, { 
          visualBriefs: readyVisualSegments,
          status: 'rendering'
        });
        await DB.log(episodeId, 'asset_generation', 'success', `Storyboard mapping completed. Compiling ${readyVisualSegments.length} slides.`);
        
        episode.visualBriefs = readyVisualSegments;
        episode.status = 'rendering';
      }

      // 3. STAGE: rendering
      if (episode.status === 'rendering') {
        this.currentStepLabel = 'Assembling multimedia audio narration...';
        this.progressAmount = 70;
        await DB.log(episodeId, 'rendering', 'info', 'Commencing final render assembly processing.');

        // Synthesize high-quality vocal audio file
        const audioUrl = await VideoAssemblyEngine.synthesizeNarration(episodeId, episode.script || '', episode.voiceName);
        
        this.currentStepLabel = 'Enforcing brand overlays and outro concats...';
        this.progressAmount = 85;

        const updatedEpisode = await DB.getEpisodeById(episodeId);
        const videoUrl = await VideoAssemblyEngine.compileFinalVideo(updatedEpisode || episode);

        await DB.updateEpisode(episodeId, {
          narrationAudioUrl: audioUrl,
          finalVideoUrl: videoUrl,
          status: 'publishing'
        });
        await DB.log(episodeId, 'rendering', 'success', 'Final publication-ready presentation compilation successfully complete.');
        
        episode.narrationAudioUrl = audioUrl;
        episode.finalVideoUrl = videoUrl;
        episode.status = 'publishing';
      }

      // 4. STAGE: publishing
      if (episode.status === 'publishing') {
        this.currentStepLabel = 'Publishing video stream to YouTube...';
        this.progressAmount = 95;
        await DB.log(episodeId, 'publishing', 'info', 'Pushing final video to official unlisted YouTube channel queue.');

        const filePath = path.join(process.cwd(), 'data', 'renders', `${episodeId}_final.mp4`);
        const result = await YouTubeService.uploadVideo({
          episodeId,
          filePath,
          title: episode.title,
          description: `Automatically compiled episode of ${episode.title}\nTopic: ${episode.topic}\n\nProduced with state-of-the-art YouTube Automation Pipelines.`,
          tags: ['automated', 'ai-generated', episode.topic.toLowerCase().replace(/\s+/g, '-')],
          privacyStatus: 'unlisted', // default unlisted for checking pipeline outputs
        });

        await DB.updateEpisode(episodeId, {
          status: 'completed',
          youtubeId: result.videoId,
          youtubeStatus: 'uploaded',
          youtubePublishDate: new Date().toISOString(),
        });
        await DB.log(episodeId, 'publishing', 'success', `Automation run fully successful. Private Video URL: https://youtube.com/watch?v=${result.videoId}`);
      }

      this.progressAmount = 100;
      this.currentStepLabel = 'Episode published!';
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

      await DB.log(episodeId, 'scheduler', 'error', `Pipeline execution critically aborted: ${errMsg}. Execution metric updated. Retry count: ${newRetries}.`);
      this.currentStepLabel = `Aborted: ${errMsg}`;
    } finally {
      this.activeProcessingId = null;
      this.isProcessingActive = false;
    }
  }

  /**
   * Safe automation scheduler that acts to sustain 7 videos per month.
   * Auto-selects planned/running episodes, recovering robustly.
   */
  static async triggerPeriodicBatchCron(): Promise<void> {
    console.log('[PIPELINE-METRICS] Weekly operational schedule check triggered.');
    const episodes = await DB.getEpisodes();
    
    // Look for any active 'failed' or state-locked pipeline items to resume on, safely.
    const resumable = episodes.find(e => e.status === 'failed' && e.retryCount < 3);
    if (resumable) {
      console.log(`[PIPELINE-METRICS] Found rescuable episode: "${resumable.title}" (Retry ${resumable.retryCount}). Auto-resuming execution...`);
      // Run async
      this.runEpisode(resumable.id).catch(err => {
        console.error('[CRON] Automated rescue runner crashed:', err);
      });
      return;
    }

    // Otherwise, discover next planned episode and process it.
    const nextPlanned = episodes.find(e => e.status === 'planned');
    if (nextPlanned) {
      console.log(`[PIPELINE-METRICS] Processing scheduled release: "${nextPlanned.title}"`);
      this.runEpisode(nextPlanned.id).catch(err => {
        console.error('[CRON] Automated releasing runner crashed:', err);
      });
      return;
    }

    console.log('[PIPELINE-METRICS] All video channels currently up-to-date. Idling safely.');
  }
}
