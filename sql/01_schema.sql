-- ============================================================
-- QEEMA (قيمة) — Database Schema  v1
-- Run once in: Supabase Dashboard > SQL Editor > New query > Run
-- Idempotent & non-destructive: safe to re-run, deletes no data.
-- ============================================================

create extension if not exists pgcrypto;  -- for gen_random_uuid()

-- ------------------------------------------------------------
-- 1) EPISODES — one row per video (the curriculum master table)
-- ------------------------------------------------------------
create table if not exists public.episodes (
  id               uuid primary key default gen_random_uuid(),
  episode_number   integer unique not null,
  surah_number     integer,            -- real Qur'an surah no. 1-114 (used by everyayah)
  surah_name       text,               -- Arabic, e.g. "الإخلاص"
  surah_name_en    text,               -- latin slug, e.g. "Al-Ikhlas"
  ayah_start       integer default 1,
  ayah_end         integer,            -- null = whole surah
  title            text,               -- Arabic video title
  status           text not null default 'planned',
  retry_count      integer not null default 0,
  error_message    text,
  youtube_video_id text,
  youtube_url      text,
  published_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- Backfill any missing columns if the table already existed
alter table public.episodes add column if not exists surah_number     integer;
alter table public.episodes add column if not exists surah_name_en    text;
alter table public.episodes add column if not exists ayah_start       integer default 1;
alter table public.episodes add column if not exists ayah_end         integer;
alter table public.episodes add column if not exists retry_count      integer not null default 0;
alter table public.episodes add column if not exists error_message    text;
alter table public.episodes add column if not exists youtube_video_id text;
alter table public.episodes add column if not exists youtube_url      text;
alter table public.episodes add column if not exists published_at     timestamptz;
alter table public.episodes add column if not exists created_at       timestamptz not null default now();
alter table public.episodes add column if not exists updated_at       timestamptz not null default now();

-- FIX: legacy 'pending' rows matched no pipeline stage -> normalise to a real start state
update public.episodes set status = 'planned' where status = 'pending' or status is null;

-- Constrain status to known states (drop-then-add so re-runs don't error)
alter table public.episodes drop constraint if exists episodes_status_chk;
alter table public.episodes add constraint episodes_status_chk
  check (status in ('planned','scripting','asset_generation','rendering','publishing','completed','failed'));

-- ------------------------------------------------------------
-- 2) PIPELINE_STATE — working artifacts produced per episode
-- ------------------------------------------------------------
create table if not exists public.pipeline_state (
  episode_id           uuid primary key references public.episodes(id) on delete cascade,
  script               text,           -- tafsir narration (Egyptian Arabic)
  reciter              text,           -- everyayah reciter folder
  voice_id             text,           -- ElevenLabs voice id
  visual_briefs        jsonb not null default '[]'::jsonb,
  recitation_audio_url text,
  narration_audio_url  text,
  thumbnail_url        text,
  final_video_url      text,
  updated_at           timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 3) PIPELINE_LOGS — audit trail (THIS TABLE WAS MISSING = the
--    "local db.json corrupt" message you kept seeing)
-- ------------------------------------------------------------
create table if not exists public.pipeline_logs (
  id          uuid primary key default gen_random_uuid(),
  episode_id  uuid references public.episodes(id) on delete cascade,
  stage       text,
  type        text not null default 'info',   -- info | success | warn | error
  message     text,
  created_at  timestamptz not null default now()
);
create index if not exists pipeline_logs_episode_idx
  on public.pipeline_logs (episode_id, created_at desc);

-- ------------------------------------------------------------
-- 4) API_KEY_METRICS — Gemini key-rotation state.
--    Moved off the local db.json file, which does NOT survive
--    between GitHub Actions runs (fresh runner every time).
-- ------------------------------------------------------------
create table if not exists public.api_key_metrics (
  key_name       text primary key,             -- KeyA | KeyB | KeyC
  requests_count integer not null default 0,
  last_used_at   timestamptz,
  status         text not null default 'active', -- active | exhausted | rate_limited
  exhausted_at   timestamptz
);
insert into public.api_key_metrics (key_name)
  values ('KeyA'), ('KeyB'), ('KeyC')
  on conflict (key_name) do nothing;

-- ------------------------------------------------------------
-- 5) updated_at auto-touch
-- ------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists episodes_touch on public.episodes;
create trigger episodes_touch before update on public.episodes
  for each row execute function public.touch_updated_at();

drop trigger if exists pipeline_state_touch on public.pipeline_state;
create trigger pipeline_state_touch before update on public.pipeline_state
  for each row execute function public.touch_updated_at();

-- Done. Next: seed the 38-episode curriculum (sent after you confirm the order).

-- ------------------------------------------------------------
-- v1.1: عمود الخطة المُهيكلة (EpisodePlan) المستخدم في الكود الجديد
-- ------------------------------------------------------------
alter table public.pipeline_state add column if not exists plan jsonb;
