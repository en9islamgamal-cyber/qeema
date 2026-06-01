/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export type EpisodeStatus =
  | 'planned'
  | 'scripting'
  | 'asset_generation'
  | 'rendering'
  | 'publishing'
  | 'completed'
  | 'failed';

export interface VisualSegment {
  timestamp: string;
  duration: number; // in seconds
  prompt: string;
  caption: string;
  assetUrl?: string; // compiled image
}

export interface Episode {
  id: string;
  title: string;
  topic: string;
  targetDate: string; // ISO string
  status: EpisodeStatus;
  script: string | null;
  voiceName: string; // 'Kore' | 'Puck' etc.
  visualBriefs: VisualSegment[];
  narrationAudioUrl: string | null;
  finalVideoUrl: string | null;
  youtubeId: string | null;
  youtubeStatus: 'none' | 'uploaded' | 'public' | 'scheduled';
  youtubePublishDate: string | null;
  retryCount: number;
  errorLog: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface ApiKeyConfig {
  id: string;
  name: string;
  keyMask: string;
  status: 'active' | 'exhausted' | 'rate_limited' | 'unconfigured';
  requestsCount: number;
  lastUsedAt: string | null;
}

export interface PipelineLog {
  id: string;
  episodeId: string | null;
  timestamp: string;
  stage: string;
  type: 'info' | 'warn' | 'error' | 'success';
  message: string;
}

export interface SystemStatus {
  isProcessing: boolean;
  activeEpisodeId: string | null;
  currentStep: string | null;
  progressPercent: number;
  monthlyQuotaUsed: number; // target: 7 episodes
}
