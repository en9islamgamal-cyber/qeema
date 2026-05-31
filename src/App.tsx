/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef } from 'react';
import {
  Video,
  Play,
  PlayCircle,
  Database,
  Logs,
  RefreshCw,
  PlusCircle,
  AlertTriangle,
  CheckCircle2,
  Calendar,
  Cpu,
  Key,
  Layers,
  Sparkles,
  Volume2,
  FileVideo,
  Trash2,
  ListRestart
} from 'lucide-react';
import { Episode, PipelineLog, ApiKeyConfig, SystemStatus } from './types.ts';

export default function App() {
  // DB States
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [selectedEpisode, setSelectedEpisode] = useState<Episode | null>(null);
  const [logs, setLogs] = useState<PipelineLog[]>([]);
  const [keys, setKeys] = useState<ApiKeyConfig[]>([]);
  const [status, setStatus] = useState<SystemStatus>({
    isProcessing: false,
    activeEpisodeId: null,
    currentStep: 'idling',
    progressPercent: 0,
    monthlyQuotaUsed: 0
  });

  // UI States
  const [newTitle, setNewTitle] = useState('');
  const [newTopic, setNewTopic] = useState('');
  const [newVoice, setNewVoice] = useState('Kore');
  const [isCreating, setIsCreating] = useState(false);
  const [isPolling, setIsPolling] = useState(true);

  // Player Playback simulator
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentSlideIndex, setCurrentSlideIndex] = useState(0);
  const [playbackTime, setPlaybackTime] = useState(0);
  const playbackIntervalRef = useRef<NodeJS.Timeout | null>(null);

  // Fetch all initial metadata with precise error handling for each endpoint
  const reloadAll = async () => {
    // 1. Fetch Episodes
    try {
      const epRes = await fetch('/api/episodes');
      const contentType = epRes.headers.get('content-type') || '';
      if (!epRes.ok) {
        console.warn(`[FRONTEND] /api/episodes returned status ${epRes.status}`);
      } else if (!contentType.includes('application/json')) {
        const text = await epRes.text();
        console.error(`[FRONTEND] Expected JSON from /api/episodes but received: "${contentType}". Body preview: ${text.slice(0, 100)}`);
      } else {
        const epData = await epRes.json();
        if (Array.isArray(epData)) {
          setEpisodes(epData);
          // Sync currently viewed item if updating
          if (selectedEpisode) {
            const fresh = epData.find(e => e.id === selectedEpisode.id);
            if (fresh) setSelectedEpisode(fresh);
          }
        }
      }
    } catch (err) {
      console.error('[FRONTEND] Failed to sync episodes:', err);
    }

    // 2. Fetch Logs
    try {
      const logRes = await fetch('/api/logs');
      const contentType = logRes.headers.get('content-type') || '';
      if (!logRes.ok) {
        console.warn(`[FRONTEND] /api/logs returned status ${logRes.status}`);
      } else if (!contentType.includes('application/json')) {
        const text = await logRes.text();
        console.error(`[FRONTEND] Expected JSON from /api/logs but received: "${contentType}". Body preview: ${text.slice(0, 100)}`);
      } else {
        const logData = await logRes.json();
        if (Array.isArray(logData)) setLogs(logData);
      }
    } catch (err) {
      console.error('[FRONTEND] Failed to sync logs:', err);
    }

    // 3. Fetch Keys
    try {
      const keyRes = await fetch('/api/keymetrics');
      const contentType = keyRes.headers.get('content-type') || '';
      if (!keyRes.ok) {
        console.warn(`[FRONTEND] /api/keymetrics returned status ${keyRes.status}`);
      } else if (!contentType.includes('application/json')) {
        const text = await keyRes.text();
        console.error(`[FRONTEND] Expected JSON from /api/keymetrics but received: "${contentType}". Body preview: ${text.slice(0, 100)}`);
      } else {
        const keyData = await keyRes.json();
        if (Array.isArray(keyData)) setKeys(keyData);
      }
    } catch (err) {
      console.error('[FRONTEND] Failed to sync API keys:', err);
    }

    // 4. Fetch Status
    try {
      const statusRes = await fetch('/api/status');
      const contentType = statusRes.headers.get('content-type') || '';
      if (!statusRes.ok) {
        console.warn(`[FRONTEND] /api/status returned status ${statusRes.status}`);
      } else if (!contentType.includes('application/json')) {
        const text = await statusRes.text();
        console.error(`[FRONTEND] Expected JSON from /api/status but received: "${contentType}". Body preview: ${text.slice(0, 100)}`);
      } else {
        const statusData = await statusRes.json();
        if (statusData && typeof statusData === 'object') {
          setStatus(statusData);
        }
      }
    } catch (err) {
      console.error('[FRONTEND] Failed to sync pipeline status:', err);
    }
  };

  // Periodic Poller
  useEffect(() => {
    reloadAll();
    const timer = setInterval(() => {
      if (isPolling) reloadAll();
    }, 4000);
    return () => clearInterval(timer);
  }, [isPolling, selectedEpisode?.id]);

  // Handle manual episode creation
  const handleCreateEpisode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim() || !newTopic.trim()) return;

    try {
      const res = await fetch('/api/episodes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: newTitle,
          topic: newTopic,
          voiceName: newVoice
        })
      });
      if (res.ok) {
        setNewTitle('');
        setNewTopic('');
        setIsCreating(false);
        await reloadAll();
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Delete episode slot
  const handleDeleteEpisode = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Are you absolutely sure you want to delete this scheduled production slot?')) return;
    try {
      const res = await fetch(`/api/episodes/${id}`, { method: 'DELETE' });
      if (res.ok) {
        if (selectedEpisode?.id === id) setSelectedEpisode(null);
        await reloadAll();
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Kickoff active automated compilation pipeline run
  const triggerPipelineRun = async (episodeId: string) => {
    try {
      const res = await fetch('/api/pipeline/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ episodeId })
      });
      if (res.ok) {
        await reloadAll();
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Seed default 7 episodes Campaign releases to hit monthly goals instantly
  const triggerCampaignSeed = async () => {
    if (!confirm('This will seed 7 technology topics for your automated campaign and schedule them 4 days apart. Continue?')) return;
    try {
      const res = await fetch('/api/pipeline/seed', { method: 'POST' });
      if (res.ok) {
        await reloadAll();
        alert('Seeded 7 production slots successfully!');
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Recover rotation keys
  const triggerResetKeysPool = async () => {
    try {
      const res = await fetch('/api/keymetrics/reset', { method: 'POST' });
      if (res.ok) {
        await reloadAll();
        alert('All Gemini quota flags successfully reset and re-synchronized.');
      }
    } catch (err) {
      console.error(err);
    }
  };

  // Audio Playback simulation controller
  useEffect(() => {
    if (isPlaying) {
      setPlaybackTime(0);
      setCurrentSlideIndex(0);
      
      const totalSlides = selectedEpisode?.visualBriefs?.length || 0;
      if (totalSlides === 0) {
        setIsPlaying(false);
        return;
      }

      playbackIntervalRef.current = setInterval(() => {
        setPlaybackTime(prev => {
          const nextVal = prev + 1;
          const currentSlide = selectedEpisode?.visualBriefs[currentSlideIndex];
          if (currentSlide && nextVal > currentSlide.duration) {
            if (currentSlideIndex + 1 < totalSlides) {
              setCurrentSlideIndex(prevIdx => prevIdx + 1);
              return 0; // Reset slide time
            } else {
              // Loop ended
              clearInterval(playbackIntervalRef.current!);
              setIsPlaying(false);
              return 0;
            }
          }
          return nextVal;
        });
      }, 1000);
    } else {
      if (playbackIntervalRef.current) clearInterval(playbackIntervalRef.current);
    }

    return () => {
      if (playbackIntervalRef.current) clearInterval(playbackIntervalRef.current);
    };
  }, [isPlaying, currentSlideIndex, selectedEpisode?.id]);

  // Statistics calculation
  const totalEpisodesSeeded = episodes.length;
  const completedEpisodesCount = episodes.filter(e => e.status === 'completed').length;
  const inProgressEpisodesCount = episodes.filter(
    e => !['planned', 'completed', 'failed'].includes(e.status)
  ).length;

  return (
    <div id="root-viewport" className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans antialiased selection:bg-purple-600 selection:text-white">
      {/* HEADER COCKPIT BAR */}
      <header className="border-b border-slate-900 bg-slate-950/80 backdrop-blur-md sticky top-0 z-50 px-6 py-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-purple-600/20 border border-purple-500/30 flex items-center justify-center text-purple-400">
            <Video className="w-6 h-6 animate-pulse" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-white flex items-center gap-2">
              YouTube Automation Platform <span className="text-xs px-2.5 py-0.5 rounded-full bg-purple-600/30 border border-purple-500/30 text-purple-300 font-mono">v2026.5</span>
            </h1>
            <p className="text-xs text-slate-400">Modular end-to-end full-stack pipeline orchestrator for video rendering and publishing</p>
          </div>
        </div>

        {/* SYSTEM ACTIVITY TRACKER */}
        <div className="flex items-center gap-4 bg-slate-900/60 border border-slate-800/80 p-2.5 rounded-xl text-xs max-w-md w-full md:w-auto">
          {status.isProcessing ? (
            <div className="flex items-center gap-3 w-full">
              <Cpu className="w-5 h-5 text-purple-400 animate-spin" />
              <div className="flex-1 min-w-[140px]">
                <div className="flex justify-between font-mono mb-1">
                  <span className="text-purple-300 font-semibold truncate">Active Pipeline</span>
                  <span className="text-slate-400">{status.progressPercent}%</span>
                </div>
                <div className="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-purple-500 to-indigo-500 transition-all duration-500"
                    style={{ width: `${status.progressPercent}%` }}
                  ></div>
                </div>
                <p className="text-[10px] text-slate-400 mt-1 italic truncate">{status.currentStep}</p>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 py-1 text-slate-400">
              <span className="w-2.5 h-2.5 rounded-full bg-slate-600 animate-pulse"></span>
              <span className="font-mono">Engine Status: Idle & Watchful</span>
            </div>
          )}
        </div>
      </header>

      {/* DASHBOARD STATISTICS HERO */}
      <section className="px-6 pt-6 grid grid-cols-2 md:grid-cols-5 gap-4">
        <div className="bg-slate-900/40 border border-slate-900 rounded-xl p-4 flex flex-col justify-between hover:border-slate-800 transition">
          <span className="text-xs text-slate-400 font-medium font-mono">Monthly Releases Target</span>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-purple-400">7</span>
            <span className="text-xs text-slate-500 font-mono">slots slated</span>
          </div>
        </div>
        <div className="bg-slate-900/40 border border-slate-900 rounded-xl p-4 flex flex-col justify-between hover:border-slate-800 transition">
          <span className="text-xs text-slate-400 font-medium font-mono font-mono">Total Campaign Episodes</span>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-white">{totalEpisodesSeeded}</span>
            <span className="text-xs text-slate-500">managed</span>
          </div>
        </div>
        <div className="bg-slate-900/40 border border-slate-900 rounded-xl p-4 flex flex-col justify-between hover:border-slate-800 transition">
          <span className="text-xs text-slate-400 font-medium font-mono">Uploaded to YouTube</span>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-green-400">{completedEpisodesCount}</span>
            <span className="text-xs text-slate-500">published</span>
          </div>
        </div>
        <div className="bg-slate-900/40 border border-slate-900 rounded-xl p-4 flex flex-col justify-between hover:border-slate-800 transition">
          <span className="text-xs text-slate-400 font-medium font-mono">Assembling / Generating</span>
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-2xl font-bold font-mono text-blue-400">{inProgressEpisodesCount}</span>
            <span className="text-xs text-slate-500">active thread</span>
          </div>
        </div>
        <div className="col-span-2 md:col-span-1 bg-gradient-to-br from-purple-950/20 to-slate-900 border border-purple-900/20 rounded-xl p-4 flex flex-col justify-between font-mono">
          <span className="text-xs text-purple-300 font-medium font-mono">Quick Seed Campaign</span>
          <button
            onClick={triggerCampaignSeed}
            className="mt-2 py-1.5 px-3 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold flex items-center justify-center gap-1.5 transition active:scale-95 font-mono"
          >
            <Sparkles className="w-4 h-4" />
            Seed 7 Releases
          </button>
        </div>
      </section>

      {/* CORE WORKSPACE PANELS */}
      <main className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-6 p-6">
        
        {/* LEFT COLUMN (GRID: 5/12): EPISODES SCHEDULE */}
        <div className="lg:col-span-5 bg-slate-900/20 border border-slate-900 rounded-2xl p-5 flex flex-col gap-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white tracking-tight flex items-center gap-2">
              <Calendar className="w-4.5 h-4.5 text-purple-400" />
              Episodes Production Queue
            </h2>
            <button
              onClick={() => setIsCreating(!isCreating)}
              className="text-xs text-purple-400 hover:text-purple-300 flex items-center gap-1 font-medium"
            >
              <PlusCircle className="w-4 h-4" />
              Add Manual Slot
            </button>
          </div>

          {/* ADD EPISODE FORM POPUP DRAWER */}
          {isCreating && (
            <form onSubmit={handleCreateEpisode} className="bg-slate-900/90 border border-slate-800 rounded-xl p-4 flex flex-col gap-3">
              <h3 className="text-xs font-semibold text-white">Configure New Broadcast Slot</h3>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] text-slate-400 font-medium">Episode Title</label>
                <input
                  type="text"
                  placeholder="e.g. Modern WebGPU Applications"
                  value={newTitle}
                  onChange={e => setNewTitle(e.target.value)}
                  className="bg-slate-950 border border-slate-800 rounded-lg py-1.5 px-3 text-xs text-white focus:outline-none focus:border-purple-500"
                  required
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] text-slate-400 font-medium">Narrative Topic / Description</label>
                <textarea
                  placeholder="Insert short prompt or focal topics for Gemini's scripting model..."
                  rows={2}
                  value={newTopic}
                  onChange={e => setNewTopic(e.target.value)}
                  className="bg-slate-950 border border-slate-800 rounded-lg py-1.5 px-3 text-xs text-white focus:outline-none focus:border-purple-500 resize-none"
                  required
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] text-slate-400 font-medium">Gemini Synthesized Narrator Voice</label>
                <select
                  value={newVoice}
                  onChange={e => setNewVoice(e.target.value)}
                  className="bg-slate-950 border border-slate-800 rounded-lg py-1.5 px-3 text-xs text-white focus:outline-none focus:border-purple-500"
                >
                  <option value="Kore">Kore (Standard Rich Tech voice)</option>
                  <option value="Puck">Puck (Fast engaging presentation voice)</option>
                  <option value="Charon">Charon (Deeps and authoritative presentation tone)</option>
                  <option value="Zephyr">Zephyr (Cheerfully warm energetic tone)</option>
                </select>
              </div>
              <div className="flex justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => setIsCreating(false)}
                  className="py-1 px-3 rounded-lg border border-slate-800 hover:bg-slate-800 text-xs font-medium"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="py-1 px-3 rounded-lg bg-purple-600 hover:bg-purple-500 text-xs font-medium text-white"
                >
                  Schedule Slot
                </button>
              </div>
            </form>
          )}

          {/* EPISODES INDEX */}
          <div className="flex-1 overflow-y-auto max-h-[500px] flex flex-col gap-2.5 pr-1">
            {episodes.length === 0 ? (
              <div className="py-12 flex flex-col items-center justify-center text-slate-400 gap-2 border border-dashed border-slate-900 rounded-xl bg-slate-950/20">
                <Database className="w-10 h-10 text-slate-700" />
                <p className="text-xs">No episodes scheduled in database yet.</p>
                <button
                  onClick={triggerCampaignSeed}
                  className="text-xs mt-2 text-purple-400 underline font-medium"
                >
                  Instantly Seed 7 Monthly Topics
                </button>
              </div>
            ) : (
              episodes.map((ep, idx) => {
                const isSelected = selectedEpisode?.id === ep.id;
                const releaseDate = new Date(ep.targetDate).toLocaleDateString('en-US', {
                  month: 'short',
                  day: 'numeric'
                });

                return (
                  <div
                    key={ep.id}
                    onClick={() => setSelectedEpisode(ep)}
                    className={`p-3.5 rounded-xl border transition-all cursor-pointer flex items-center justify-between gap-3 ${
                      isSelected
                        ? 'bg-purple-950/15 border-purple-500/40 shadow-sm relative'
                        : 'bg-slate-900/30 border-slate-900 hover:border-slate-800 hover:bg-slate-900/50'
                    }`}
                  >
                    <div className="flex-1 min-w-0 flex items-start gap-3">
                      <span className="font-mono text-xs text-slate-500 mt-0.5">#{String(idx + 1).padStart(2, '0')}</span>
                      <div className="flex-1 min-w-0">
                        <h3 className="text-xs font-medium text-white truncate">{ep.title}</h3>
                        <p className="text-[10px] text-slate-400 truncate mt-0.5">{ep.topic}</p>
                        <div className="flex items-center gap-2 mt-2">
                          <span className="text-[10px] text-slate-500 flex items-center gap-1 font-mono">
                            <Calendar className="w-3.5 h-3.5" />
                            {releaseDate}
                          </span>
                          <span className="w-1.5 h-1.5 rounded-full bg-slate-800"></span>
                          <span className="text-[10px] text-slate-500 font-mono">Voice: {ep.voiceName}</span>
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center gap-2">
                      {/* Interactive Actions per State */}
                      {ep.status === 'planned' && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            triggerPipelineRun(ep.id);
                          }}
                          className="py-1 px-2.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-[10px] font-semibold flex items-center gap-1 active:scale-95"
                        >
                          <Play className="w-3 h-3 fill-current animate-pulse ml-0.5" />
                          Launch
                        </button>
                      )}

                      {ep.status === 'failed' && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            triggerPipelineRun(ep.id);
                          }}
                          className="py-1 px-2 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-[10px] font-semibold flex items-center gap-1"
                        >
                          <RefreshCw className="w-3 h-3" />
                          Rescue
                        </button>
                      )}

                      {/* Status pill visuals */}
                      <span
                        className={`text-[9px] px-2 py-0.5 rounded-full font-sans uppercase font-bold border ${
                          ep.status === 'completed'
                            ? 'bg-green-500/10 border-green-500/20 text-green-400'
                            : ep.status === 'failed'
                            ? 'bg-red-500/10 border-red-500/20 text-red-500'
                            : ep.status === 'planned'
                            ? 'bg-slate-800 border-slate-700 text-slate-400'
                            : 'bg-purple-500/10 border-purple-500/20 text-purple-400 animate-pulse'
                        }`}
                      >
                        {ep.status}
                      </span>

                      {/* Delete */}
                      <button
                        onClick={(e) => handleDeleteEpisode(ep.id, e)}
                        className="p-1 rounded text-slate-600 hover:text-red-400 hover:bg-slate-800 transition"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* RIGHT COLUMN (GRID: 7/12): ACTIONABLE WORKFLOW STATE VIEW */}
        <div className="lg:col-span-7 flex flex-col gap-6">
          
          {/* CURRENT ACTIVE VISUAL STORYBOARD PREVIEW & AUDIO SYNTH PLAYBACK */}
          {selectedEpisode ? (
            <div className="bg-slate-900/20 border border-slate-900 rounded-2xl p-5 flex flex-col gap-4">
              <div className="flex flex-col md:flex-row md:items-center justify-between border-b border-slate-900 pb-3 gap-3">
                <div>
                  <h2 className="text-sm font-semibold text-white tracking-tight flex items-center gap-2">
                    <Layers className="w-4.5 h-4.5 text-purple-400" />
                    Episode Console: "{selectedEpisode.title}"
                  </h2>
                  <p className="text-[10px] mt-0.5 text-slate-400 font-mono">ID: {selectedEpisode.id}</p>
                </div>

                <div className="flex items-center gap-2">
                  {/* Master Release Buttons */}
                  <button
                    onClick={() => triggerPipelineRun(selectedEpisode.id)}
                    className="py-1.5 px-3 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold flex items-center gap-1.5 active:scale-95"
                  >
                    <Cpu className="w-4 h-4" />
                    Force Render Run
                  </button>
                </div>
              </div>

              {/* STAGE OVERLAYS TRACKER */}
              <div className="grid grid-cols-4 gap-2 text-center text-[10px] font-medium">
                <div className={`p-2 rounded-lg border ${selectedEpisode.script ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-slate-950 border-slate-900 text-slate-500'}`}>
                  1. Scripting Done
                </div>
                <div className={`p-2 rounded-lg border ${selectedEpisode.visualBriefs?.length > 0 ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-slate-950 border-slate-900 text-slate-500'}`}>
                  2. Storyboards Done
                </div>
                <div className={`p-2 rounded-lg border ${selectedEpisode.narrationAudioUrl ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-slate-950 border-slate-900 text-slate-500'}`}>
                  3. Rendering Done
                </div>
                <div className={`p-2 rounded-lg border ${selectedEpisode.status === 'completed' ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-slate-950 border-slate-900 text-slate-500'}`}>
                  4. YouTube Uploaded
                </div>
              </div>

              {/* MOCK PLAYER VIEW COKCPIT */}
              <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 flex flex-col gap-3">
                <div className="aspect-video w-full rounded-lg bg-slate-900 relative overflow-hidden flex items-center justify-center border border-slate-800">
                  {selectedEpisode.visualBriefs?.length > 0 ? (
                    <div className="absolute inset-0 flex flex-col">
                      <div className="flex-1 bg-slate-950 flex items-center justify-center text-center relative p-8">
                        {/* Render Slide Image Fallback Graphic */}
                        <div className="absolute inset-0 bg-gradient-to-tr from-slate-950 via-purple-950/20 to-indigo-950/10 flex items-center justify-center">
                          <img
                            src={selectedEpisode.visualBriefs[currentSlideIndex]?.assetUrl || `https://placehold.co/600x400/121827/FFFFFF/png?text=Frame+Index+${currentSlideIndex}`}
                            alt="Visual illustration Slide preview"
                            className="max-h-full max-w-full object-contain rounded-md"
                            onError={(e) => {
                              (e.target as any).src = 'https://placehold.co/600x400/121827/FFFFFF/png?text=Storyboard+Frame';
                            }}
                          />
                        </div>

                        {/* Caption Watermark overlay */}
                        <div className="absolute bottom-6 left-1/2 -translate-x-1/2 max-w-lg bg-black/75 px-3 py-1.5 rounded-md border border-slate-800 text-xs font-medium text-slate-200 shadow-md">
                          {selectedEpisode.visualBriefs[currentSlideIndex]?.caption}
                        </div>

                        {/* Bottom water-mark overlay branded channel identity */}
                        <div className="absolute top-4 right-4 text-[10px] font-bold text-slate-400 tracking-wider font-mono flex items-center gap-1.5 bg-black/60 py-1 px-2.5 rounded border border-slate-800">
                          <span className="w-2 h-2 rounded-full bg-purple-500"></span>
                          BRAND LOGO OVERLAY ACTIVE
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="text-center p-6 text-slate-500 flex flex-col items-center gap-2">
                      <FileVideo className="w-12 h-12 text-slate-800" />
                      <p className="text-xs">Visual frames not yet processed. Trigger Render to compile slides.</p>
                    </div>
                  )}
                </div>

                {/* Player Controls bar */}
                <div className="flex items-center justify-between bg-slate-900/40 p-2.5 rounded-lg border border-slate-900 text-xs">
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => setIsPlaying(!isPlaying)}
                      disabled={!selectedEpisode.visualBriefs || selectedEpisode.visualBriefs.length === 0}
                      className="w-8 h-8 rounded-full bg-purple-600 flex items-center justify-center text-white font-bold hover:bg-purple-500 active:scale-95 disabled:bg-slate-800 disabled:text-slate-500"
                    >
                      {isPlaying ? (
                        <span className="w-2.5 h-2.5 bg-white rounded-sm block font-mono"></span>
                      ) : (
                        <Play className="w-3.5 h-3.5 fill-current ml-0.5" />
                      )}
                    </button>
                    <div>
                      <p className="font-semibold text-white">Interactive Render Player</p>
                      <p className="text-[10px] text-slate-400 font-mono">
                        {selectedEpisode.visualBriefs?.length > 0
                          ? `Frame Slide ${currentSlideIndex + 1}/${selectedEpisode.visualBriefs.length} (${playbackTime}s / ${selectedEpisode.visualBriefs[currentSlideIndex]?.duration || 0}s)`
                          : 'Empty storyboard'}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-4 text-[11px] font-mono text-slate-400">
                    <div className="flex items-center gap-1 text-slate-300">
                      <Volume2 className="w-4 h-4 text-purple-400" />
                      <span className="underline">TTS Audio Narration</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* MULTILINE SCRIPTING SCRIPT VIEW CARD */}
              <div className="bg-slate-950 border border-slate-900 rounded-xl p-4 flex flex-col gap-2">
                <span className="text-xs font-semibold text-slate-400">Synthesized Spoken Script Narrative:</span>
                <p className="text-xs text-slate-200 leading-relaxed italic bg-slate-900/30 p-3 rounded-lg border border-slate-900 min-h-[50px]">
                  {selectedEpisode.script || 'Script has not been created yet. Launch this scheduled slot to draft script via Gemini.'}
                </p>
              </div>

              {/* TIMELINE VISUAL MODULES */}
              {selectedEpisode.visualBriefs?.length > 0 && (
                <div className="flex flex-col gap-2">
                  <span className="text-xs font-semibold text-slate-400">Storyboard Frame Breakdown:</span>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {selectedEpisode.visualBriefs.map((br, i) => (
                      <div
                        key={i}
                        onClick={() => setCurrentSlideIndex(i)}
                        className={`p-2.5 rounded-lg border cursor-pointer text-[11px] transition ${
                          currentSlideIndex === i
                            ? 'bg-purple-950/10 border-purple-500/40'
                            : 'bg-slate-950 border-slate-900 hover:border-slate-800'
                        }`}
                      >
                        <div className="flex justify-between text-[10px] text-slate-400 font-mono font-semibold mb-1">
                          <span>Slide #{i + 1}</span>
                          <span>{br.timestamp} ({br.duration}s)</span>
                        </div>
                        <p className="text-white font-medium truncate mb-1">Caption: "{br.caption}"</p>
                        <p className="text-[10px] text-slate-400 line-clamp-2">Prompt: "{br.prompt}"</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* AUDIT FAILURE LOG WRAPPER IF FAILS */}
              {selectedEpisode.status === 'failed' && selectedEpisode.errorLog && (
                <div className="bg-red-950/20 border border-red-500/30 text-rose-300 p-3.5 rounded-xl text-xs flex gap-2">
                  <AlertTriangle className="w-4.5 h-4.5 shrink-0 text-red-400 mt-0.5" />
                  <div>
                    <span className="font-bold">Execution Error Logged:</span>
                    <p className="font-mono mt-1 text-[11px] leading-relaxed bg-black/30 p-2.5 rounded-md text-red-400">{selectedEpisode.errorLog}</p>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex-1 bg-slate-900/10 border border-slate-900 border-dashed rounded-2xl p-12 flex flex-col items-center justify-center text-slate-500 text-center gap-3">
              <PlayCircle className="w-14 h-14 text-slate-800 animate-pulse" />
              <h3 className="text-sm font-semibold text-slate-300">No Episode Selected</h3>
              <p className="text-xs max-w-sm">Select an episode pipeline on the left cockpit queue to view visual storyboards, play TTS narrations, verify overlays, and diagnose operational runs.</p>
            </div>
          )}

          {/* ACTIVE LIVE LOGS PANEL */}
          <div className="bg-slate-900/20 border border-slate-900 rounded-2xl p-4 flex flex-col gap-3">
            <h2 className="text-xs font-semibold text-white tracking-tight flex items-center gap-1.5 border-b border-slate-950 pb-2">
              <Logs className="w-4 h-4 text-purple-400" />
              Live Pipeline Logs Diagnostics Console
            </h2>

            <div className="bg-slate-950 border border-slate-900 rounded-xl p-3 font-mono text-[10.5px] max-h-[160px] overflow-y-auto flex flex-col gap-1.5">
              {logs.length === 0 ? (
                <span className="text-slate-600 block text-center py-4">Logs stream idle. Ready for operations.</span>
              ) : (
                logs.map((log) => {
                  const stamp = new Date(log.timestamp).toLocaleTimeString();
                  return (
                    <div key={log.id} className="flex gap-2 items-start leading-relaxed hover:bg-slate-900/30 p-0.5 rounded transition">
                      <span className="text-slate-500 shrink-0 font-medium">[{stamp}]</span>
                      <span
                        className={`shrink-0 font-bold uppercase text-[9px] px-1 py-0.5 rounded leading-none ${
                          log.type === 'success'
                            ? 'bg-green-500/15 text-green-400'
                            : log.type === 'error'
                            ? 'bg-red-500/15 text-red-500'
                            : log.type === 'warn'
                            ? 'bg-amber-500/15 text-amber-400'
                            : 'bg-slate-800 text-slate-300'
                        }`}
                      >
                        {log.stage}
                      </span>
                      <span className="text-slate-300 flex-1">{log.message}</span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </main>

      {/* FOOTER: KEY ROTATION POOL & BRAND SAFETY SETUP PANEL */}
      <footer className="mt-auto border-t border-slate-900 bg-slate-950 p-6 flex flex-col lg:flex-row gap-6 justify-between items-start text-xs text-slate-400">
        
        {/* KEY ROTATIONS BLOCK */}
        <div className="flex-1 w-full lg:max-w-xl flex flex-col gap-3">
          <div className="flex items-center justify-between border-b border-slate-900 pb-1.5">
            <h3 className="text-xs font-semibold text-slate-200 flex items-center gap-1.5 font-mono">
              <Key className="w-4 h-4 text-purple-400" />
              Quota-Aware Gemini Key Pools
            </h3>
            <button
              onClick={triggerResetKeysPool}
              className="text-[10px] text-purple-400 hover:text-purple-300 font-medium flex items-center gap-1 font-mono"
            >
              <ListRestart className="w-3.5 h-3.5" />
              Reset Pool Statuses
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {keys.map((k) => (
              <div key={k.id} className="bg-slate-900/40 border border-slate-900 rounded-lg p-2.5 flex flex-col justify-between gap-1.5">
                <div className="flex justify-between items-start font-mono">
                  <span className="font-semibold text-slate-200 font-mono">{k.id === 'KeyA' ? 'Primary Key' : k.id === 'KeyB' ? 'Backup Key B' : 'Sandbox Key C'}</span>
                  <span
                    className={`text-[9px] font-mono px-1.5 py-0.2 rounded font-bold ${
                      k.status === 'active'
                        ? 'bg-green-500/10 text-green-400'
                        : k.status === 'rate_limited'
                        ? 'bg-yellow-500/15 text-yellow-500'
                        : 'bg-slate-800 text-slate-400'
                    }`}
                  >
                    {k.status}
                  </span>
                </div>
                <div className="font-mono text-[10px]">
                  <p className="text-slate-500">{k.keyMask}</p>
                  <p className="text-slate-400 mt-1 font-semibold">{k.requestsCount} reqs ledgered</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* IDENTITY ASSETS SAFETY NOTICE */}
        <div className="p-4 bg-slate-900/30 border border-slate-900 rounded-xl max-w-lg w-full">
          <h3 className="font-semibold text-slate-200 flex items-center gap-1.5 mb-2">
            <Database className="w-4 h-4 text-purple-400" />
            Media Storage & Channel Identity Information
          </h3>
          <p className="leading-relaxed text-slate-400 text-[11px]">
            The automation pipeline checks for brand assets in the <strong>/assets/</strong> directory. To brand your weekly releases persistently:
          </p>
          <ul className="list-disc list-inside mt-2 text-slate-400 text-[11px] space-y-1">
            <li>Place your PNG watermark watermark on: <code className="text-purple-400">/assets/logo.png</code></li>
            <li>Place your MP4 append outro on: <code className="text-purple-400">/assets/outro.mp4</code></li>
            <li>Enable cloud database syncing inside: <code className="text-purple-400">.env</code></li>
          </ul>
        </div>
      </footer>
    </div>
  );
}