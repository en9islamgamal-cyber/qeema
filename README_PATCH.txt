═══════════════════════════════════════════════════════════════
QEEMA v2 — Code Patch (Yesterday + Today)
═══════════════════════════════════════════════════════════════

This ZIP contains ONLY code files (7 files).
The assets/logo.png and assets/outro.mp4 you'll upload yourself.

═══════════════════════════════════════════════════════════════
HOW TO APPLY
═══════════════════════════════════════════════════════════════

1. Extract this ZIP locally.

2. Upload your asset files separately to GitHub:
   📁 assets/logo.png    (your channel logo PNG)
   📁 assets/outro.mp4   (your outro animation MP4)

3. Upload each code file from this ZIP to its location in
   the GitHub repo, REPLACING the old file:

   📁 core/config.py
   📁 pipeline/prompts.py
   📁 video/video_assembler.py
   📁 video/thumbnail_builder.py
   📁 assets_engines/elevenlabs_client.py
   📄 main.py                                (in repo root)
   📁 .github/workflows/pipeline.yml

4. Commit message:
   "patch: all session changes (tashkeel revert + outro + thumbnail + logo sizes)"

5. Run Episode 2 with force=true.

═══════════════════════════════════════════════════════════════
WHAT EACH FILE CHANGED
═══════════════════════════════════════════════════════════════

═════ FROM YESTERDAY (the fixes that made Episode 1 work) ═════

⚙️  core/config.py — yesterday's fix preserved:
    • _optional_env() handles BLANK env vars (was crashing on
      empty ELEVENLABS_VOICE_ID secret)

⚙️  assets_engines/elevenlabs_client.py — yesterday's fix:
    • Defensive check: raises clear error if voice_id is empty
      instead of producing a broken URL

═════ FROM TODAY (new improvements) ═════

⚙️  core/config.py — additionally tweaked today:
    • elevenlabs_speed       : 0.95  → 1.05 (faster for kids)
    • logo_overlay_width     : 180   → 320  (much bigger watermark)
    • logo_overlay_opacity   : 0.75  → 0.85
    • logo_overlay_margin    : 30    → 40
    • logo_intro_width       : 420   → 600  (huge intro splash)
    • logo_intro_duration_sec: 2.0   → 2.5
    • + OUTRO_VIDEO_PATH constant added (points to assets/outro.mp4)

📝  pipeline/prompts.py — major rewrite:
    • REMOVED all tashkeel/diacritic rules (was making TTS WORSE)
    • Plain Egyptian Arabic — no harakat
    • Visual style is HARD-CODED in Python (not the LLM)
    • Gemini outputs scene descriptions only
    • Code combines scene + style → consistent visuals guaranteed
    • Limited color palette to channel colors only

🎬  video/video_assembler.py — new feature:
    • _append_outro_animation() function added
    • Converts outro.mp4 from 9:16 vertical → 16:9 horizontal
      using blurred-background fill (looks professional)
    • 0.8s audio + video crossfade
    • Appended after outro narration
    • Skips gracefully if outro.mp4 not in assets/

🖼️  video/thumbnail_builder.py — complete rewrite:
    • Template-based design (was simple resize before)
    • Logo bottom-left at 400px (was 240px)
    • Dark gradient overlay for text readability
    • Title text rendered with Amiri Bold Arabic
    • White text + thick black outline
    • Falls back to logo-only if font unavailable

📄  main.py — small change:
    • Passes bundle.narration.title to thumbnail builder

⚙️  .github/workflows/pipeline.yml — added Amiri:
    • Installs fonts-amiri (Arabic font for thumbnails)
    • Copies amiri-bold.ttf to assets/Amiri-Bold.ttf

═══════════════════════════════════════════════════════════════
WHAT TO EXPECT IN THE NEXT EPISODE
═══════════════════════════════════════════════════════════════

🎙️  Audio:
    - Egyptian Arabic without tashkeel
    - 5% faster delivery

🎨  Visuals:
    - All scenes share the same watercolor+ink style
    - Each scene tied to its specific ayah's meaning

🌟  Logo (the one you upload as assets/logo.png):
    - 320px watermark in every scene
    - 600px intro splash
    - 400px on thumbnails

🎬  Outro (the outro.mp4 you upload):
    - Appended at the very end of the video
    - Smooth 0.8s crossfade transition
    - 9:16 → 16:9 conversion with blurred background

🖼️  Thumbnails:
    - Episode title in Amiri Bold Arabic
    - Dark gradient overlay
    - 3 variants for A/B testing
