-- ═══════════════════════════════════════════════════════════════
-- QEEMA / VALUE v11.0 — Supabase Schema
-- ═══════════════════════════════════════════════════════════════
-- Run this once in your Supabase SQL editor to set up the tables.
--
-- Tables:
--   episodes        : one row per pipeline run (1:1 with curriculum)
--   pipeline_state  : stage-level state for resume after failure
-- ═══════════════════════════════════════════════════════════════

-- ── Enable UUID generation ────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Table: episodes ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS episodes (
    id              UUID            PRIMARY KEY  DEFAULT uuid_generate_v4(),
    episode_number  INTEGER         NOT NULL UNIQUE,
    status          TEXT            NOT NULL DEFAULT 'pending',
    surah           TEXT,
    youtube_url     TEXT,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT episodes_status_check CHECK (status IN (
        'pending',
        'processing',
        'completed',
        'failed',
        'failed_quality',
        'failed_permanent'
    ))
);

CREATE INDEX IF NOT EXISTS idx_episodes_status
    ON episodes(status);

CREATE INDEX IF NOT EXISTS idx_episodes_number
    ON episodes(episode_number);

-- ── Table: pipeline_state ────────────────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_state (
    episode_id      UUID            NOT NULL REFERENCES episodes(id)
                                             ON DELETE CASCADE,
    stage           TEXT            NOT NULL,
    state_data      JSONB,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (episode_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_state_stage
    ON pipeline_state(stage);

-- ── updated_at trigger ────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at_column()
    RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_episodes_updated_at ON episodes;
CREATE TRIGGER trigger_episodes_updated_at
    BEFORE UPDATE ON episodes
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ── Seed all 38 episodes as pending ───────────────────────────
INSERT INTO episodes (episode_number, status)
SELECT generate_series(1, 38), 'pending'
ON CONFLICT (episode_number) DO NOTHING;
