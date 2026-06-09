-- ============================================================
-- QEEMA (قيمة) — Database Schema  v2 (FIXED)
-- يصلّح: pipeline_state القديم بشكل غلط + pipeline_logs المفقود + episodes.retry_count
-- آمن: لا يمسّ بيانات جدول episodes (المنهج). يعيد إنشاء pipeline_state/pipeline_logs
-- (دول بيتولّدوا كل تشغيل، مفيش داتا مهمة فيهم).
-- شغّله في: Supabase > SQL Editor > New query > Run
-- ============================================================

create extension if not exists pgcrypto;

-- ------------------------------------------------------------
-- 1) EPISODES — نضيف الأعمدة الناقصة فقط (بدون مساس بالبيانات)
-- ------------------------------------------------------------
create table if not exists public.episodes (
  id uuid primary key default gen_random_uuid(),
  episode_number integer unique not null
);
alter table public.episodes add column if not exists surah_number     integer;
alter table public.episodes add column if not exists surah_name       text;
alter table public.episodes add column if not exists surah_name_en    text;
alter table public.episodes add column if not exists ayah_start       integer default 1;
alter table public.episodes add column if not exists ayah_end         integer;
alter table public.episodes add column if not exists title            text;
alter table public.episodes add column if not exists status           text not null default 'planned';
alter table public.episodes add column if not exists retry_count      integer not null default 0;
alter table public.episodes add column if not exists error_message    text;
alter table public.episodes add column if not exists youtube_video_id text;
alter table public.episodes add column if not exists youtube_url      text;
alter table public.episodes add column if not exists published_at     timestamptz;
alter table public.episodes add column if not exists created_at       timestamptz not null default now();
alter table public.episodes add column if not exists updated_at       timestamptz not null default now();

update public.episodes set status = 'planned' where status is null or status = 'pending';

alter table public.episodes drop constraint if exists episodes_status_chk;
alter table public.episodes add constraint episodes_status_chk
  check (status in ('planned','scripting','asset_generation','rendering','publishing','completed','failed'));

-- ------------------------------------------------------------
-- 2) PIPELINE_STATE — إعادة إنشاء بالشكل الصح (آمن: بيانات مؤقتة)
-- ------------------------------------------------------------
drop table if exists public.pipeline_state cascade;
create table public.pipeline_state (
  episode_id           uuid primary key references public.episodes(id) on delete cascade,
  plan                 jsonb,
  script               text,
  reciter              text,
  voice_id             text,
  visual_briefs        jsonb not null default '[]'::jsonb,
  recitation_audio_url text,
  narration_audio_url  text,
  thumbnail_url        text,
  final_video_url      text,
  updated_at           timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 3) PIPELINE_LOGS — إعادة إنشاء (كان مفقود)
-- ------------------------------------------------------------
drop table if exists public.pipeline_logs cascade;
create table public.pipeline_logs (
  id          uuid primary key default gen_random_uuid(),
  episode_id  uuid references public.episodes(id) on delete cascade,
  stage       text,
  type        text not null default 'info',
  message     text,
  created_at  timestamptz not null default now()
);
create index pipeline_logs_episode_idx on public.pipeline_logs (episode_id, created_at desc);

-- ------------------------------------------------------------
-- 4) API_KEY_METRICS — تدوير مفاتيح Gemini
-- ------------------------------------------------------------
create table if not exists public.api_key_metrics (
  key_name       text primary key,
  requests_count integer not null default 0,
  last_used_at   timestamptz,
  status         text not null default 'active',
  exhausted_at   timestamptz
);
insert into public.api_key_metrics (key_name) values ('KeyA'),('KeyB'),('KeyC')
  on conflict (key_name) do nothing;

-- ------------------------------------------------------------
-- 5) updated_at auto-touch
-- ------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists episodes_touch on public.episodes;
create trigger episodes_touch before update on public.episodes
  for each row execute function public.touch_updated_at();

drop trigger if exists pipeline_state_touch on public.pipeline_state;
create trigger pipeline_state_touch before update on public.pipeline_state
  for each row execute function public.touch_updated_at();

-- يطلب من PostgREST إعادة تحميل كاش السكيمة فورًا (يحل "schema cache" errors)
notify pgrst, 'reload schema';
