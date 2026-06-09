/** QEEMA — أنواع مشتركة */
import { SurahInput } from './prompts.ts';

export type EpisodeStatus =
  | 'planned'
  | 'scripting'
  | 'asset_generation'
  | 'rendering'
  | 'publishing'
  | 'completed'
  | 'failed';

export interface Episode {
  id: string;
  episodeNumber: number;
  surahNumber: number;
  surahName: string;
  surahNameEn: string;
  ayahStart: number;
  ayahEnd: number | null;
  title: string | null;
  status: EpisodeStatus;
  retryCount: number;
  errorMessage: string | null;
  youtubeVideoId: string | null;
}

export function episodeToSurah(ep: Episode): SurahInput {
  return {
    surahNumber: ep.surahNumber,
    surahName: ep.surahName,
    surahNameEn: ep.surahNameEn,
    ayahStart: ep.ayahStart || 1,
    ayahEnd: ep.ayahEnd,
  };
}
