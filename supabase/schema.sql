-- FACELESS AGENT SUPABASE SCHEMA
-- Run in Supabase SQL Editor: app.supabase.com

CREATE TABLE IF NOT EXISTS topics (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    topic TEXT NOT NULL,
    hook TEXT,
    angle TEXT,
    emotion TEXT DEFAULT 'inspiration',
    virality_score DECIMAL(3,1) DEFAULT 5.0,
    niche TEXT NOT NULL,
    source TEXT,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS videos (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    video_id TEXT UNIQUE NOT NULL,
    title TEXT,
    topic TEXT,
    niche TEXT NOT NULL,
    youtube_url TEXT,
    quality_score DECIMAL(4,2) DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS analytics (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    video_id TEXT REFERENCES videos(video_id),
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    fetched_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS batch_runs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    run_date DATE DEFAULT CURRENT_DATE,
    total_videos INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    niche TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
