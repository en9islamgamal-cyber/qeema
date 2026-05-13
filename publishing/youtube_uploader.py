"""
publishing/youtube_uploader.py
====================================================================
Uploads the final episode to YouTube.

Uses OAuth 2.0 refresh tokens (long-lived) to authenticate without
interactive flow — perfect for CI/CD.

Workflow:
  1. Use refresh_token to get a fresh access_token
  2. Upload video with metadata (resumable)
  3. Set thumbnail

If thumbnail upload fails (it's optional but auto-generated thumbs
may not pass YT's policy), the video still uploads successfully.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

from core.config import get_api_keys, get_pipeline_config


log = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
YOUTUBE_THUMBNAIL_URL = (
    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
)


class YouTubeError(Exception):
    pass


@dataclass
class UploadResult:
    video_id: str
    video_url: str
    thumbnail_uploaded: bool


class YouTubeUploader:

    def __init__(self) -> None:
        self.keys = get_api_keys()
        self.cfg = get_pipeline_config()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ─────────────────────────────────────────────────────────────
    # OAuth: refresh access token
    # ─────────────────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        log.info("Refreshing YouTube access token...")
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self.keys.youtube_client_id,
                "client_secret": self.keys.youtube_client_secret,
                "refresh_token": self.keys.youtube_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise YouTubeError(f"Token refresh failed: {r.status_code} {r.text[:300]}")

        data = r.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = time.time() + expires_in
        log.info("✓ Token refreshed, valid for %ds", expires_in)
        return self._access_token

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: List[str],
        thumbnail_path: Optional[Path] = None,
    ) -> UploadResult:
        """Upload video + optionally set thumbnail."""
        token = self._get_access_token()

        # ─── Step 1: Initiate resumable upload session ───────────
        snippet = {
            "title": title[:100],  # YouTube title limit
            "description": description[:5000],  # YouTube description limit
            "tags": tags[:15],
            "categoryId": self.cfg.youtube_category_id,
            "defaultLanguage": self.cfg.youtube_default_lang,
            "defaultAudioLanguage": self.cfg.youtube_default_lang,
        }
        status = {
            "privacyStatus": self.cfg.youtube_privacy,
            "selfDeclaredMadeForKids": True,  # mandatory for kids content
            "embeddable": True,
        }
        metadata = {"snippet": snippet, "status": status}

        size = video_path.stat().st_size
        log.info(
            "Initiating YT upload: %s (%d bytes, %.1f MB)",
            video_path.name, size, size / 1024 / 1024,
        )

        init_response = requests.post(
            YOUTUBE_UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            json=metadata,
            timeout=30,
        )
        if init_response.status_code != 200:
            raise YouTubeError(
                f"Upload init failed: {init_response.status_code} "
                f"{init_response.text[:300]}"
            )
        upload_url = init_response.headers.get("location") or init_response.headers.get("Location")
        if not upload_url:
            raise YouTubeError("No upload URL in init response")

        # ─── Step 2: Upload the video bytes (resumable) ──────────
        log.info("Uploading video bytes...")
        with video_path.open("rb") as f:
            put_response = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": "video/mp4"},
                timeout=600,  # 10 min for large videos
            )

        if put_response.status_code not in (200, 201):
            raise YouTubeError(
                f"Upload failed: {put_response.status_code} "
                f"{put_response.text[:300]}"
            )

        body = put_response.json()
        video_id = body.get("id")
        if not video_id:
            raise YouTubeError(f"No video id in response: {body}")

        video_url = f"https://youtu.be/{video_id}"
        log.info("✅ Video uploaded: %s", video_url)

        # ─── Step 3: Thumbnail (optional, best-effort) ───────────
        thumbnail_ok = False
        if thumbnail_path and thumbnail_path.exists():
            try:
                self._set_thumbnail(video_id, thumbnail_path)
                thumbnail_ok = True
            except Exception as e:
                log.warning("Thumbnail upload failed: %s", e)

        return UploadResult(
            video_id=video_id,
            video_url=video_url,
            thumbnail_uploaded=thumbnail_ok,
        )

    # ─────────────────────────────────────────────────────────────
    # Internal: thumbnail
    # ─────────────────────────────────────────────────────────────

    def _set_thumbnail(self, video_id: str, thumbnail: Path) -> None:
        token = self._get_access_token()
        with thumbnail.open("rb") as f:
            r = requests.post(
                f"{YOUTUBE_THUMBNAIL_URL}?videoId={video_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "image/jpeg",
                },
                data=f,
                timeout=120,
            )
        if r.status_code != 200:
            raise YouTubeError(
                f"Thumbnail set failed: {r.status_code} {r.text[:300]}"
            )
        log.info("✓ Thumbnail set for video %s", video_id)
