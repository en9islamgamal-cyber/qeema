/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { createClient, SupabaseClient } from '@supabase/supabase-js';
import * as fs from 'fs';
import * as path from 'path';
import { Episode, PipelineLog, EpisodeStatus } from '../src/types.ts';

const DB_DIR = path.join(process.cwd(), 'data');
const DB_FILE = path.join(DB_DIR, 'db.json');

// Polyfill WebSocket for Node.js < 22 where globalThis.WebSocket is not defined,
// to prevent Supabase Realtime client instantiation from throwing an error.
if (typeof globalThis.WebSocket === 'undefined') {
  (globalThis as any).WebSocket = class MockWebSocket {
    static CLOSED = 3;
    static CLOSING = 2;
    static CONNECTING = 0;
    static OPEN = 1;
    constructor() {
      throw new Error('Supabase Realtime WebSockets are not initialized in this environment.');
    }
  };
}

// Initialize Supabase if variables are configured
let supabase: SupabaseClient | null = null;
const supabaseUrl = process.env.SUPABASE_URL || '';
const supabaseKey = process.env.SUPABASE_KEY || process.env.SUPABASE_ANON_KEY || '';

if (supabaseUrl && supabaseKey) {
  try {
    supabase = createClient(supabaseUrl, supabaseKey);
    console.log('[DB] Supabase database client successfully initialized.');
  } catch (error) {
    console.error('[DB] Failed primary initialization of Supabase client, falling back to local files.', error);
  }
} else {
  console.log('[DB] No Supabase credentials detected. Running in elegant file-backed Local Mode.');
}

// Structured Local Database Types
interface LocalDbSchema {
  episodes: Episode[];
  logs: PipelineLog[];
  keysStatus: {
    [keyId: string]: {
      requestsCount: number;
      lastUsedAt: string | null;
      status: 'active' | 'exhausted' | 'rate_limited';
    };
  };
}

const DEFAULT_DB: LocalDbSchema = {
  episodes: [],
  logs: [],
  keysStatus: {
    KeyA: { requestsCount: 0, lastUsedAt: null, status: 'active' },
    KeyB: { requestsCount: 0, lastUsedAt: null, status: 'active' },
    KeyC: { requestsCount: 0, lastUsedAt: null, status: 'active' },
  },
};

// Guarantee synchronous db.json creation
function ensureLocalDbExists(): LocalDbSchema {
  if (!fs.existsSync(DB_DIR)) {
    fs.mkdirSync(DB_DIR, { recursive: true });
  }
  if (!fs.existsSync(DB_FILE)) {
    fs.writeFileSync(DB_FILE, JSON.stringify(DEFAULT_DB, null, 2), 'utf-8');
    return DEFAULT_DB;
  }
  try {
    const raw = fs.readFileSync(DB_FILE, 'utf-8');
    return JSON.parse(raw) as LocalDbSchema;
  } catch (err) {
    console.error('[DB] local db.json corrupt. Restoring default backup schema.', err);
    fs.writeFileSync(DB_FILE, JSON.stringify(DEFAULT_DB, null, 2), 'utf-8');
    return DEFAULT_DB;
  }
}

function writeLocalDb(data: LocalDbSchema): void {
  try {
    fs.writeFileSync(DB_FILE, JSON.stringify(data, null, 2), 'utf-8');
  } catch (err) {
    console.error('[DB] Write error on local file-system:', err);
  }
}

function mapDbStatusToAppStatus(status: string | null): Episode['status'] {
  if (!status || status === 'pending') return 'planned';
  return status as Episode['status'];
}

function mapDbRowToEpisode(epRow: any, psRow?: any): Episode {
  const merged = { ...epRow, ...psRow };
  return {
    id: merged.id,
    title: merged.title || (merged.surah_name ? `سورة ${merged.surah_name}` : 'Untitled Episode'),
    topic: merged.surah_name ? `سورة ${merged.surah_name}` : (merged.topic || merged.title || 'Islamic Topic'),
    targetDate: merged.published_at || merged.targetDate || new Date().toISOString(),
    status: mapDbStatusToAppStatus(merged.status),
    script: merged.script || null,
    voiceName: merged.voice_name || merged.voiceName || 'Kore',
    visualBriefs: Array.isArray(merged.visual_briefs) ? merged.visual_briefs : (Array.isArray(merged.visualBriefs) ? merged.visualBriefs : []),
    narrationAudioUrl: merged.narration_audio_url || merged.narrationAudioUrl || null,
    finalVideoUrl: merged.video_path || merged.finalVideoUrl || null,
    youtubeId: merged.youtube_video_id || merged.youtubeId || null,
    youtubeStatus: merged.youtube_video_id ? 'uploaded' : 'none',
    youtubePublishDate: merged.published_at || merged.youtubePublishDate || null,
    retryCount: typeof merged.retry_count === 'number' ? merged.retry_count : (typeof merged.retryCount === 'number' ? merged.retryCount : 0),
    errorLog: merged.error_message || merged.errorLog || null,
    createdAt: merged.created_at || merged.createdAt || new Date().toISOString(),
    updatedAt: merged.updated_at || merged.updatedAt || new Date().toISOString(),
  };
}

export class DB {
  // EPISODES ACTIONS
  static async getEpisodes(): Promise<Episode[]> {
    if (supabase) {
      const { data, error } = await supabase
        .from('episodes')
        .select('*')
        .order('episode_number', { ascending: true });
      if (!error && data) {
        const hydrated: Episode[] = [];
        for (const row of data) {
          let psRow: any = null;
          try {
            const { data: qPs } = await supabase
              .from('pipeline_state')
              .select('*')
              .eq('episode_id', row.id)
              .maybeSingle();
            psRow = qPs;
          } catch (e) {
            // ignore table/column mismatches defensively
          }

          if (!psRow) {
            const cachePath = path.join(DB_DIR, `state_${row.id}.json`);
            if (fs.existsSync(cachePath)) {
              try {
                psRow = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
              } catch (err) {}
            }
          }
          hydrated.push(mapDbRowToEpisode(row, psRow));
        }
        return hydrated;
      }
      console.error('[DB] Supabase episodes fetch error, reading local file-safe fallback:', error);
    }
    const db = ensureLocalDbExists();
    return db.episodes;
  }

  static async getEpisodeById(id: string): Promise<Episode | null> {
    const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
    const parsedInt = parseInt(id, 10);
    const isNumeric = !isNaN(parsedInt) && String(parsedInt) === String(id).trim();
    
    const fieldQueried = isUuid ? 'id' : (isNumeric ? 'episode_number' : 'id');
    const valueQueried = isUuid ? id : (isNumeric ? parsedInt : id);

    console.log(`[DB DEBUG] getEpisodeById received input: "${id}"`);
    console.log(`[DB DEBUG] Treated as: ${isUuid ? 'UUID' : (isNumeric ? 'Numeric Episode Number' : 'Fallback/ID/UUID')}`);
    console.log(`[DB DEBUG] Querying table: episodes, field: "${fieldQueried}", value: ${JSON.stringify(valueQueried)}`);

    if (supabase) {
      let query = supabase.from('episodes').select('*');
      if (isUuid) {
        query = query.eq('id', id);
      } else if (isNumeric) {
        query = query.eq('episode_number', valueQueried);
      } else {
        query = query.eq('id', id);
      }

      const { data: row, error: epErr } = await query.maybeSingle();
      if (epErr) {
        console.error(`[DB DEBUG] Supabase query error:`, epErr);
      }
      
      console.log(`[DB DEBUG] Rows returned from Supabase:`, row ? 1 : 0);

      if (!epErr && row) {
        let psRow: any = null;
        try {
          const { data: qPs } = await supabase
            .from('pipeline_state')
            .select('*')
            .eq('episode_id', row.id)
            .maybeSingle();
          psRow = qPs;
        } catch (e) {}

        if (!psRow) {
          const cachePath = path.join(DB_DIR, `state_${row.id}.json`);
          if (fs.existsSync(cachePath)) {
            try {
              psRow = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
            } catch (err) {}
          }
        }
        return mapDbRowToEpisode(row, psRow);
      }
    } else {
      console.log(`[DB DEBUG] Supabase client is not initialized, querying local DB file.`);
    }

    const db = ensureLocalDbExists();
    let found = db.episodes.find((e) => e.id === id);
    if (!found && isNumeric) {
      found = db.episodes.find((e) => (e as any).episode_number === parsedInt) || db.episodes[parsedInt - 1] || null;
    }
    
    console.log(`[DB DEBUG] Rows returned from local DB fallback:`, found ? 1 : 0);
    return found || null;
  }

  static async createEpisode(episode: Omit<Episode, 'id' | 'createdAt' | 'updatedAt' | 'retryCount'>): Promise<Episode> {
    const id = crypto.randomUUID();
    const now = new Date().toISOString();
    
    let nextNum = 1;
    if (supabase) {
      try {
        const { data } = await supabase
          .from('episodes')
          .select('episode_number')
          .order('episode_number', { ascending: false })
          .limit(1);
        if (data && data[0] && typeof data[0].episode_number === 'number') {
          nextNum = data[0].episode_number + 1;
        }
      } catch (e) {}
    } else {
      const db = ensureLocalDbExists();
      if (db.episodes.length > 0) {
        nextNum = db.episodes.length + 1;
      }
    }

    const newEpisode: Episode = {
      ...episode,
      id,
      retryCount: 0,
      createdAt: now,
      updatedAt: now,
    };

    if (supabase) {
      try {
        const epData = {
          id,
          episode_number: nextNum,
          status: 'pending',
          title: episode.title,
          video_path: episode.finalVideoUrl || null,
          youtube_video_id: episode.youtubeId || null,
          published_at: episode.targetDate || now,
        };

        const { data, error } = await supabase
          .from('episodes')
          .insert([epData])
          .select()
          .single();

        if (!error && data) {
          try {
            await supabase.from('pipeline_state').insert([{
              episode_id: id,
              script: episode.script,
              voice_name: episode.voiceName || 'Kore',
              visual_briefs: episode.visualBriefs || [],
              narration_audio_url: episode.narrationAudioUrl || null,
              final_video_url: episode.finalVideoUrl || null,
            }]);
          } catch (psErr) {}
          return mapDbRowToEpisode(data);
        }
        console.error('[DB] Supabase insert failed, logging locally:', error);
      } catch (e) {
        console.error('[DB] Supabase insert error:', e);
      }
    }

    const db = ensureLocalDbExists();
    db.episodes.push(newEpisode);
    writeLocalDb(db);
    return newEpisode;
  }

  static async updateEpisode(id: string, updates: Partial<Episode>): Promise<Episode> {
    const now = new Date().toISOString();
    
    let realUuid = id;
    let originalEpisode: Episode | null = null;
    
    if (supabase) {
      originalEpisode = await this.getEpisodeById(id);
      if (originalEpisode) {
        realUuid = originalEpisode.id;
      }
    }

    if (supabase && originalEpisode) {
      const epUpdates: any = {
        updated_at: now,
      };

      if (updates.status !== undefined) epUpdates.status = updates.status;
      if (updates.title !== undefined) epUpdates.title = updates.title;
      if (updates.finalVideoUrl !== undefined) epUpdates.video_path = updates.finalVideoUrl;
      if (updates.visualBriefs && updates.visualBriefs.length > 0) {
        epUpdates.thumbnail_path = updates.visualBriefs[0].assetUrl || null;
      }
      if (updates.youtubeId !== undefined) {
        epUpdates.youtube_video_id = updates.youtubeId;
        epUpdates.youtube_url = `https://youtube.com/watch?v=${updates.youtubeId}`;
      }
      if (updates.youtubePublishDate !== undefined) epUpdates.published_at = updates.youtubePublishDate;
      if (updates.errorLog !== undefined) {
        epUpdates.error_message = updates.errorLog;
        epUpdates.error_tracker = updates.errorLog?.slice(0, 200) || null;
      }

      const { data: epData, error: epErr } = await supabase
        .from('episodes')
        .update(epUpdates)
        .eq('id', realUuid)
        .select()
        .single();
      
      if (epErr) {
        console.error('[DB] Supabase primary episodes table update failed:', epErr);
      }

      const psUpdates: any = {};
      if (updates.script !== undefined) psUpdates.script = updates.script;
      if (updates.voiceName !== undefined) psUpdates.voice_name = updates.voiceName;
      if (updates.visualBriefs !== undefined) psUpdates.visual_briefs = updates.visualBriefs;
      if (updates.narrationAudioUrl !== undefined) psUpdates.narration_audio_url = updates.narrationAudioUrl;
      if (updates.finalVideoUrl !== undefined) psUpdates.final_video_url = updates.finalVideoUrl;

      let psRow: any = null;
      if (Object.keys(psUpdates).length > 0) {
        try {
          const { data: existingPs } = await supabase
            .from('pipeline_state')
            .select('*')
            .eq('episode_id', realUuid)
            .maybeSingle();

          if (existingPs) {
            const { data: updatedPs } = await supabase
              .from('pipeline_state')
              .update(psUpdates)
              .eq('episode_id', realUuid)
              .select()
              .single();
            psRow = updatedPs;
          } else {
            const { data: insertedPs } = await supabase
              .from('pipeline_state')
              .insert([{ episode_id: realUuid, ...psUpdates }])
              .select()
              .single();
            psRow = insertedPs;
          }
        } catch (e: any) {
          console.warn('[DB] Supabase pipeline_state write failed, relying on local cache:', e?.message);
        }

        try {
          const localCachePath = path.join(DB_DIR, `state_${realUuid}.json`);
          let cachedState: any = {};
          if (fs.existsSync(localCachePath)) {
            try { cachedState = JSON.parse(fs.readFileSync(localCachePath, 'utf8')); } catch (e) {}
          }
          const finalCachedState = { ...cachedState, ...psUpdates };
          fs.writeFileSync(localCachePath, JSON.stringify(finalCachedState, null, 2), 'utf8');
          if (!psRow) {
            psRow = finalCachedState;
          }
        } catch (err) {
          console.error('[DB] Failed saving local cache file:', err);
        }
      }

      if (epData) {
        return mapDbRowToEpisode(epData, psRow);
      }
    }

    const db = ensureLocalDbExists();
    const index = db.episodes.findIndex((e) => e.id === realUuid);
    if (index === -1) {
      throw new Error(`Episode with target ID/number "${id}" not found.`);
    }

    db.episodes[index] = {
      ...db.episodes[index],
      ...updates,
      updatedAt: now,
    };
    writeLocalDb(db);
    return db.episodes[index];
  }

  static async deleteEpisode(id: string): Promise<boolean> {
    if (supabase) {
      const { error } = await supabase.from('episodes').delete().eq('id', id);
      try {
        await supabase.from('pipeline_state').delete().eq('episode_id', id);
      } catch (e) {}
      if (!error) return true;
    }
    const db = ensureLocalDbExists();
    const initialLen = db.episodes.length;
    db.episodes = db.episodes.filter((e) => e.id !== id);
    writeLocalDb(db);
    return db.episodes.length < initialLen;
  }

  // AUDIT LOGS
  static async getLogs(episodeId?: string): Promise<PipelineLog[]> {
    if (supabase) {
      let query = supabase.from('pipeline_logs').select('*').order('timestamp', { ascending: false });
      if (episodeId) {
        query = query.eq('episodeId', episodeId);
      }
      const { data: logData, error: logError } = await query;
      if (!logError && logData) return logData as PipelineLog[];
    }
    const db = ensureLocalDbExists();
    if (episodeId) {
      return db.logs.filter((l) => l.episodeId === episodeId).slice().reverse();
    }
    return db.logs.slice().reverse();
  }

  static async log(episodeId: string | null, stage: string, type: PipelineLog['type'], message: string): Promise<PipelineLog> {
    const newLog: PipelineLog = {
      id: crypto.randomUUID(),
      episodeId,
      timestamp: new Date().toISOString(),
      stage,
      type,
      message,
    };

    console.log(`[PIPELINE][${stage?.toUpperCase()}][${type.toUpperCase()}] ${message}`);

    if (supabase) {
      const { error } = await supabase.from('pipeline_logs').insert([newLog]);
      if (!error) return newLog;
    }

    const db = ensureLocalDbExists();
    db.logs.push(newLog);
    // Prune logs if they get excessively large (keep last 1000 items)
    if (db.logs.length > 1000) {
      db.logs.shift();
    }
    writeLocalDb(db);
    return newLog;
  }

  // KEY ROTATION SERVICE METRICS
  static async getKeyMetric(keyId: string) {
    const db = ensureLocalDbExists();
    return db.keysStatus[keyId] || { requestsCount: 0, lastUsedAt: null, status: 'active' };
  }

  static async trackKeyUsage(keyId: string, status?: 'active' | 'exhausted' | 'rate_limited'): Promise<void> {
    const db = ensureLocalDbExists();
    if (!db.keysStatus[keyId]) {
      db.keysStatus[keyId] = { requestsCount: 0, lastUsedAt: null, status: 'active' };
    }
    db.keysStatus[keyId].requestsCount += 1;
    db.keysStatus[keyId].lastUsedAt = new Date().toISOString();
    if (status) {
      db.keysStatus[keyId].status = status;
    }
    writeLocalDb(db);
  }

  static async makeKeyActive(keyId: string): Promise<void> {
    const db = ensureLocalDbExists();
    if (db.keysStatus[keyId]) {
      db.keysStatus[keyId].status = 'active';
      writeLocalDb(db);
    }
  }

  static async markKeyExhausted(keyId: string): Promise<void> {
    const db = ensureLocalDbExists();
    if (db.keysStatus[keyId]) {
      db.keysStatus[keyId].status = 'exhausted';
      writeLocalDb(db);
    }
  }
}
