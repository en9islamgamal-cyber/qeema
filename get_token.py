"""
get_token.py — Run ONCE locally to obtain a YouTube refresh token.

[Usage]
1. In Google Cloud Console, create OAuth 2.0 credentials
   (Desktop app type) and download as credentials.json
2. Place credentials.json next to this file
3. Run: python get_token.py
4. A browser window opens; sign in with the YouTube channel owner
5. Copy the printed values into your env / GitHub Secrets

This is a one-time setup script. The pipeline never calls it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        print("❌ google-auth-oauthlib not installed", file=sys.stderr)
        print("   Run: pip install google-auth-oauthlib", file=sys.stderr)
        return 1

    creds_file = Path(__file__).parent / "credentials.json"
    if not creds_file.exists():
        print(f"❌ Missing {creds_file}", file=sys.stderr)
        print(
            "   Download OAuth credentials (Desktop type) from Google Cloud "
            "Console and save as 'credentials.json' next to this script.",
            file=sys.stderr,
        )
        return 2

    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        print(
            "❌ No refresh_token returned. Try again and approve the consent.",
            file=sys.stderr,
        )
        return 3

    raw = json.loads(creds_file.read_text())
    client_info = raw.get("installed") or raw.get("web") or {}

    print("\n" + "═" * 64)
    print("✅ SUCCESS — copy these into your env or GitHub Secrets:")
    print("═" * 64)
    print(f"YOUTUBE_CLIENT_ID={client_info.get('client_id', '')}")
    print(f"YOUTUBE_CLIENT_SECRET={client_info.get('client_secret', '')}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("═" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
