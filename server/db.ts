/**
 * QEEMA — Data layer (Supabase)
 * - جداول: episodes, pipeline_state, pipeline_logs, api_key_metrics
 * - لا يعتمد على أي ملف محلي (كان ده سبب رسالة db.json corrupt القديمة).
 * - يفشل بصوت عالٍ: لو Supabase مش متظبّط -> Error واضح.
 */
import { createClient, SupabaseClient } from '@supabase/supabase-js';
import { SUPABASE } from './config.ts';
import { Episode, EpisodeStatus } from './types.ts';
import { EpisodePlan } from './prompts.ts';

// Node 20 مفيهوش WebSocket عام؛ نمنع Realtime من رمي خطأ عند الإنشاء.
if (typeof (globalThis as any).WebSocket === 'undefined') {
  (globalThis as any).WebSocket = class {
    constructor() {
      throw new Error('Realtime WebSocket not used in this environment.');
    }
  };
}

let _client: SupabaseClient | null = null;
function db(): SupabaseClient {
  if (!_client) {
    _client = createClient(SUPABASE.url(), SUPABASE.key(), {
      auth: { persistSession: false },
    });
  }
  return _client;
}

function rowToEpisode(r: any): Episode {
  return {
    id: r.id,
    episodeNumber: r.episode_number,
    surahNumber: r.surah_number,
    surahName: r.surah_name,
    surahNameEn: r.surah_name_en,
    ayahStart: r.ayah_start ?? 1,
    ayahEnd: r.ayah_end ?? null,
    title: r.title ?? null,
    status: (r.status ?? 'planned') as EpisodeStatus,
    retryCount: r.retry_count ?? 0,
    errorMessage: r.error_message ?? null,
    youtubeVideoId: r.youtube_video_id ?? null,
  };
}

export const DB = {
  async getEpisodeById(idOrNumber: string): Promise<Episode | null> {
    const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(idOrNumber);
    const n = parseInt(idOrNumber, 10);
    const isNumeric = !isNaN(n) && String(n) === idOrNumber.trim();

    let q = db().from('episodes').select('*');
    q = isUuid ? q.eq('id', idOrNumber) : isNumeric ? q.eq('episode_number', n) : q.eq('id', idOrNumber);

    const { data, error } = await q.maybeSingle();
    if (error) throw new Error(`[db] getEpisodeById فشل: ${error.message}`);
    return data ? rowToEpisode(data) : null;
  },

  async getEpisodes(): Promise<Episode[]> {
    const { data, error } = await db()
      .from('episodes')
      .select('*')
      .order('episode_number', { ascending: true });
    if (error) throw new Error(`[db] getEpisodes فشل: ${error.message}`);
    return (data || []).map(rowToEpisode);
  },

  async setStatus(id: string, status: EpisodeStatus): Promise<void> {
    const { error } = await db().from('episodes').update({ status }).eq('id', id);
    if (error) throw new Error(`[db] setStatus فشل: ${error.message}`);
  },

  async markFailed(id: string, message: string, retryCount: number): Promise<void> {
    const { error } = await db()
      .from('episodes')
      .update({ status: 'failed', error_message: message, retry_count: retryCount })
      .eq('id', id);
    if (error) console.error(`[db] markFailed فشل: ${error.message}`);
  },

  async setTitle(id: string, title: string): Promise<void> {
    const { error } = await db().from('episodes').update({ title }).eq('id', id);
    if (error) throw new Error(`[db] setTitle فشل: ${error.message}`);
  },

  async setPublished(id: string, youtubeVideoId: string): Promise<void> {
    const { error } = await db()
      .from('episodes')
      .update({
        status: 'completed',
        youtube_video_id: youtubeVideoId,
        youtube_url: `https://youtube.com/watch?v=${youtubeVideoId}`,
        published_at: new Date().toISOString(),
      })
      .eq('id', id);
    if (error) throw new Error(`[db] setPublished فشل: ${error.message}`);
  },

  async savePlan(episodeId: string, plan: EpisodePlan): Promise<void> {
    const payload = { episode_id: episodeId, plan };
    const { error } = await db().from('pipeline_state').upsert(payload, { onConflict: 'episode_id' });
    if (error) throw new Error(`[db] savePlan فشل: ${error.message}`);
  },

  async saveFinalVideoUrl(episodeId: string, url: string): Promise<void> {
    const { error } = await db()
      .from('pipeline_state')
      .upsert({ episode_id: episodeId, final_video_url: url }, { onConflict: 'episode_id' });
    if (error) console.error(`[db] saveFinalVideoUrl فشل: ${error.message}`);
  },

  /** سجل مرحلة في pipeline_logs + اطبعها (loud). لا يكسر التشغيل لو فشل التسجيل. */
  async log(episodeId: string | null, stage: string, type: 'info' | 'success' | 'warn' | 'error', message: string): Promise<void> {
    console.log(`[${stage.toUpperCase()}][${type.toUpperCase()}] ${message}`);
    try {
      const { error } = await db().from('pipeline_logs').insert([{ episode_id: episodeId, stage, type, message }]);
      if (error) console.error(`[db] تعذّر كتابة اللوج في Supabase: ${error.message}`);
    } catch (e: any) {
      console.error(`[db] استثناء أثناء كتابة اللوج: ${e?.message || e}`);
    }
  },

  /* ---------- تدوير مفاتيح Gemini عبر api_key_metrics ---------- */
  async getKeyStatus(keyName: string): Promise<string> {
    const { data } = await db().from('api_key_metrics').select('status').eq('key_name', keyName).maybeSingle();
    return data?.status || 'active';
  },
  async trackKeyUsage(keyName: string): Promise<void> {
    const { data } = await db().from('api_key_metrics').select('requests_count').eq('key_name', keyName).maybeSingle();
    const count = (data?.requests_count || 0) + 1;
    await db().from('api_key_metrics').upsert(
      { key_name: keyName, requests_count: count, last_used_at: new Date().toISOString() },
      { onConflict: 'key_name' }
    );
  },
  async markKeyExhausted(keyName: string): Promise<void> {
    await db().from('api_key_metrics').upsert(
      { key_name: keyName, status: 'exhausted', exhausted_at: new Date().toISOString() },
      { onConflict: 'key_name' }
    );
  },
  async resetAllKeys(): Promise<void> {
    await db().from('api_key_metrics').update({ status: 'active' }).neq('key_name', '');
  },
};
