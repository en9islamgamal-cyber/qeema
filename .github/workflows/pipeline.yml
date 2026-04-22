name: "VALUE QEEMA Pipeline"

on:
  schedule:
    - cron: "0 3 * * 1,4"
  workflow_dispatch:
    inputs:
      episode_number:
        description: "Episode number"
        required: false
        type: string

jobs:
  pipeline:
    name: "Produce Episode"
    runs-on: ubuntu-22.04
    timeout-minutes: 180
    
    steps:
      - name: "Checkout Code"
        uses: actions/checkout@v4
        
      - name: "Setup Python"
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: "Install System Dependencies"
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y ffmpeg fonts-noto-core libfreetype6-dev

      - name: "Create Required Directories"
        run: |
          # هذه الخطوة تحل مشكلة FileNotFoundError نهائياً
          mkdir -p logs temp/episodes/{tts_cache,quran_audio_cache} temp/assembly
          mkdir -p output/{videos,shorts} assets/{fonts,music,sfx,overlays,thumbnails}

      - name: "Install Python Packages"
        run: pip install -r requirements.txt -q

      - name: "Run Pipeline"
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          COHERE_API_KEY: ${{ secrets.COHERE_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
          LEONARDO_API_KEY: ${{ secrets.LEONARDO_API_KEY }}
          YOUTUBE_CLIENT_ID: ${{ secrets.YOUTUBE_CLIENT_ID }}
          YOUTUBE_CLIENT_SECRET: ${{ secrets.YOUTUBE_CLIENT_SECRET }}
          YOUTUBE_REFRESH_TOKEN: ${{ secrets.YOUTUBE_REFRESH_TOKEN }}
        run: |
          ARGS=""
          if [ -n "${{ github.event.inputs.episode_number }}" ]; then
            ARGS="--episode ${{ github.event.inputs.episode_number }}"
          fi
          
          # نظام إعادة التشغيل التلقائي الذكي
          for i in {1..3}; do
            echo "🚀 Attempt $i..."
            if python main.py $ARGS; then
              echo "✅ Success!"
              break
            else
              echo "⚠️ Failure. Retrying in 60s..."
              sleep 60
            fi
          done

      - name: "Upload Artifacts"
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: episode-results-${{ github.run_number }}
          path: |
            output/videos/*.mp4
            logs/*.log