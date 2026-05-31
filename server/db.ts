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

export class DB {
  // EPISODES ACTIONS
  static async getEpisodes(): Promise<Episode[]> {
    if (supabase) {
      const { data, error } = await supabase
        .from('episodes')
        .select('*')
        .order('targetDate', { ascending: true });
      if (!error && data) return data as Episode[];
      console.error('[DB] Supabase episodes fetch error, reading local file-safe fallback:', error);
    }
    const db = ensureLocalDbExists();
    return db.episodes;
  }

  static async getEpisodeById(id: string): Promise<Episode | null> {
    if (supabase) {
      const { data, error } = await supabase
        .from('episodes')
        .select('*')
        .eq('id', id)
        .maybeSingle();
      if (!error && data) return data as Episode;
    }
    const db = ensureLocalDbExists();
    return db.episodes.find((e) => e.id === id) || null;
  }

  static async createEpisode(episode: Omit<Episode, 'id' | 'createdAt' | 'updatedAt' | 'retryCount'>): Promise<Episode> {
    const id = crypto.randomUUID();
    const now = new Date().toISOString();
    const newEpisode: Episode = {
      ...episode,
      id,
      retryCount: 0,
      createdAt: now,
      updatedAt: now,
    };

    if (supabase) {
      const { data, error } = await supabase
        .from('episodes')
        .insert([newEpisode])
        .select()
        .single();
      if (!error && data) return data as Episode;
      console.error('[DB] Supabase insert failed, logging locally as primary runtime state:', error);
    }

    const db = ensureLocalDbExists();
    db.episodes.push(newEpisode);
    writeLocalDb(db);
    return newEpisode;
  }

  static async updateEpisode(id: string, updates: Partial<Episode>): Promise<Episode> {
    const now = new Date().toISOString();
    
    if (supabase) {
      const { data, error } = await supabase
        .from('episodes')
        .update({ ...updates, updatedAt: now })
        .eq('id', id)
        .select()
        .single();
      if (!error && data) return data as Episode;
      console.error('[DB] Supabase update failed, tracking state changes in local file-store.', error);
    }

    const db = ensureLocalDbExists();
    const index = db.episodes.findIndex((e) => e.id === id);
    if (index === -1) {
      throw new Error(`Episode with id ${id} not found to perform state transition.`);
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
      const { data, error } = await supabase.from('pipeline_logs').insert([newLog]).select().single();
      if (!error && data) return data as PipelineLog;
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
