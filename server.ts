/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import express from 'express';
import path from 'path';
import fs from 'fs';
import { createServer as createViteServer } from 'vite';
import { DB } from './server/db.ts';
import { PipelineOrchestrator } from './server/orchestrator.ts';
import { VideoAssemblyEngine } from './server/video-assembly.ts';

// Config and constants
const PORT = 3000;
const app = express();

app.use(express.json());

// 1. API ROUTES

// Serve static assets from our local rendering folders with proper mime-types
app.use('/api/renders', express.static(path.join(process.cwd(), 'data', 'renders')));

// EPISODES ENDPOINTS
app.get('/api/episodes', async (req, res) => {
  try {
    const data = await DB.getEpisodes();
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err?.message || 'Failed to list episodes' });
  }
});

app.get('/api/episodes/:id', async (req, res) => {
  try {
    const data = await DB.getEpisodeById(req.params.id);
    if (!data) return res.status(404).json({ error: 'Episode not found' });
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

app.post('/api/episodes', async (req, res) => {
  try {
    const { title, topic, targetDate, voiceName } = req.body;
    if (!title || !topic) {
      return res.status(400).json({ error: 'Title and topic are required parameters' });
    }

    const data = await DB.createEpisode({
      title,
      topic,
      targetDate: targetDate || new Date(Date.now() + 7 * 86400000).toISOString(),
      status: 'planned',
      script: null,
      voiceName: voiceName || 'Kore',
      visualBriefs: [],
      narrationAudioUrl: null,
      finalVideoUrl: null,
      youtubeId: null,
      youtubeStatus: 'none',
      youtubePublishDate: null,
      errorLog: null,
    });
    
    await DB.log(data.id, 'scheduler', 'info', `Created manual planned episode: "${title}"`);
    res.status(201).json(data);
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

app.put('/api/episodes/:id', async (req, res) => {
  try {
    const data = await DB.updateEpisode(req.params.id, req.body);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

app.delete('/api/episodes/:id', async (req, res) => {
  try {
    const success = await DB.deleteEpisode(req.params.id);
    res.json({ success });
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

// PIPELINE STATE CONTROL
app.get('/api/status', async (req, res) => {
  const state = PipelineOrchestrator.getActiveState();
  res.json(state);
});

app.get('/api/logs', async (req, res) => {
  try {
    const id = req.query.episodeId as string | undefined;
    const data = await DB.getLogs(id);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

// Trigger pipeline launch
app.post('/api/pipeline/run', async (req, res) => {
  try {
    const { episodeId } = req.body;
    if (!episodeId) return res.status(400).json({ error: 'Missing episodeId parameter' });

    // Non-blocking trigger so that client receives instantaneous status and polls progress
    PipelineOrchestrator.runEpisode(episodeId).catch((err) => {
      console.error('[SERVER] Background render agent error:', err);
    });

    res.json({ status: 'initiated', message: 'Production pipeline started in worker Thread.' });
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

// Key Pool diagnostics
app.get('/api/keys', async (req, res) => {
  try {
    const keys = ['KeyA', 'KeyB', 'KeyC'];
    const results = [];
    for (const k of keys) {
      const metric = await DB.getKeyMetric(k);
      results.push({
        id: k,
        name: k === 'KeyA' ? 'Primary Account Key' : k === 'KeyB' ? 'Backup Key Pool 2' : 'Tertiary Sandbox Key',
        keyMask: process.env[k === 'KeyA' ? 'GEMINI_API_KEY' : k === 'KeyB' ? 'GEMINI_API_KEY_2' : 'GEMINI_API_KEY_3'] 
          ? '••••••••••••••••' 
          : 'not-configured',
        status: metric.status,
        requestsCount: metric.requestsCount,
        lastUsedAt: metric.lastUsedAt,
      });
    }
    res.json(results);
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

// Re-evaluate key pools
app.post('/api/keys/reset', async (req, res) => {
  try {
    await DB.makeKeyActive('KeyA');
    await DB.makeKeyActive('KeyB');
    await DB.makeKeyActive('KeyC');
    res.json({ status: 'ok', message: 'All key statuses successfully rejuvenated to ACTIVE state.' });
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});

// Clean and Seed 7 Episodes for default monthly schedule release
app.post('/api/pipeline/seed', async (req, res) => {
  try {
    const seedTopics = [
      { title: 'The Rise of Quantum Computing', topic: 'How qubits, superposition, and entanglement are redefining high performance computing in 2026.' },
      { title: 'Deep Dive into Next-Gen LLMs', topic: 'Explaining reasoning models, reinforcement learning, and agentic scaling properties.' },
      { title: 'Rust vs Go in Modern Microservices', topic: 'A complete comparative analysis of performance profiles, safety features, and developer velocity.' },
      { title: 'WebGPU: Graphics in Your Browser', topic: 'How to harness local hardware GPU rendering pipelines with client-side JavaScript.' },
      { title: 'Understanding Vector Databases', topic: 'An intuitive explanation of embedding vectors, high-dimensional spaces, and cosine searches.' },
      { title: 'Docker Internals Explained', topic: 'How namespaces, cgroups, and overlay file systems construct isolated secure containers.' },
      { title: 'Designing Event-Driven Microservices', topic: 'Best practices for messaging protocols using Kafka, RabbitMQ, and transaction logs.' }
    ];

    const results = [];
    const baseTime = Date.now();
    
    for (let idx = 0; idx < seedTopics.length; idx++) {
      const item = seedTopics[idx];
      const targetDate = new Date(baseTime + (idx * 4) * 86400000).toISOString(); // releasing every 4 days to easily hit ~7 videos
      const check = await DB.createEpisode({
        title: item.title,
        topic: item.topic,
        targetDate,
        status: 'planned',
        script: null,
        voiceName: idx % 2 === 0 ? 'Kore' : 'Puck',
        visualBriefs: [],
        narrationAudioUrl: null,
        finalVideoUrl: null,
        youtubeId: null,
        youtubeStatus: 'none',
        youtubePublishDate: null,
        errorLog: null,
      });
      results.push(check);
    }
    res.json({ seededCount: results.length, data: results });
  } catch (err: any) {
    res.status(500).json({ error: err?.message });
  }
});


// 2. FRONT-END SERVING AND BUNDLING INTEROPERABILITY

async function startServer() {
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
    console.log('[SERVER] Bundled Vite development middlewares mounted successfully.');
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
    console.log('[SERVER] Native production build static handlers configured.');
  }

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`[SERVER] YouTube Automation Channel System successfully running on http://localhost:${PORT}`);
  });
}

startServer().catch((err) => {
  console.error('[SERVER] Critical server initial crash aborted operation:', err);
});
