"""
infrastructure/youtube_uploader.py — VALUE / QEEMA v11.0 (Production)
=========================================================================
YouTube Data API v3 uploader with:
  - Robust OAuth token refresh (no stale-token failures)
  - Resumable chunked upload with retry
  - Thumbnail upload
  - Proper error classification

[Why a self-contained module?]
- The orchestrator should not know about Google libraries
- Token caching is per-process; 60-min validity is enforced here
- Upload failures must be classifiable (network vs auth vs quota)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Final, Optional

import requests

from core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NetworkError,
    QuotaExceededError,
    UploadError,
)
from core.interfaces import UploadRequest, UploadResult, VideoUploader

logger = logging.getLogger(__name__)


_OAUTH_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"
_TOKEN_BUFFER_SEC: Final[float] = 60.0


# ════════════════════════════════════════════════════════════════
# Token manager
# ════════════════════════════════════════════════════════════════
class _YouTubeTokenManager:
    """OAuth refresh-token manager with in-memory caching."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        if not all([client_id, client_secret, refresh_token]):
            raise ConfigurationError(
                "YouTube credentials incomplete (need CLIENT_ID, "
                "CLIENT_SECRET, REFRESH_TOKEN)"
            )
        self._client_id: str = client_id
        self._client_secret: str = client_secret
        self._refresh_token: str = refresh_token
        self._cached_token: str = ""
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        now = time.time()
        if self._cached_token and now < (self._expires_at - _TOKEN_BUFFER_SEC):
            return self._cached_token

        logger.info("🔄 Refreshing YouTube access token")
        try:
            resp = requests.post(
                _OAUTH_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
        except requests.RequestException as e:
            raise NetworkError(f"YouTube token refresh failed: {e}", cause=e) from e

        if resp.status_code == 401 or resp.status_code == 400:
            raise AuthenticationError(
                f"YouTube token refresh denied "
                f"(HTTP {resp.status_code}): {resp.text[:200]}"
            )
        if resp.status_code != 200:
            raise NetworkError(
                f"YouTube token refresh HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        data = resp.json()
        self._cached_token = data["access_token"]
        self._expires_at = now + float(data.get("expires_in", 3600))
        logger.info(f"✅ Token refreshed (valid {data.get('expires_in', 3600)}s)")
        return self._cached_token


# ════════════════════════════════════════════════════════════════
# YouTubeUploader
# ════════════════════════════════════════════════════════════════
class YouTubeUploader(VideoUploader):
    """
    Production YouTube uploader.
    Uses google-api-python-client for resumable uploads.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        chunk_size_mb: int = 5,
        max_retries: int = 5,
        default_language: str = "ar",
    ) -> None:
        self._token_manager = _YouTubeTokenManager(
            client_id, client_secret, refresh_token
        )
        self._chunk_size: int = chunk_size_mb * 1024 * 1024
        self._max_retries: int = max_retries
        self._default_language: str = default_language

    def upload(self, request: UploadRequest) -> UploadResult:
        if not Path(request.video_path).exists():
            raise UploadError(
                f"Video file missing: {request.video_path}",
                video_path=request.video_path,
            )

        try:
            from googleapiclient.discovery import build  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
            from googleapiclient.errors import HttpError  # type: ignore
            import google.oauth2.credentials  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-api-python-client not installed"
            ) from e

        token = self._token_manager.get_access_token()
        creds = google.oauth2.credentials.Credentials(token=token)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

        body = {
            "snippet": {
                "title": request.title[:100],
                "description": request.description[:5000],
                "tags": request.tags[:20],
                "categoryId": request.category_id,
                "defaultLanguage": self._default_language,
            },
            "status": {
                "privacyStatus": request.privacy,
                "selfDeclaredMadeForKids": request.made_for_kids,
            },
        }
        media = MediaFileUpload(
            request.video_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=self._chunk_size,
        )
        insert = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        # ── Resumable upload loop with retry
        response: Optional[dict] = None
        retries = 0
        while response is None:
            try:
                status, response = insert.next_chunk()
                if status:
                    logger.info(
                        f"📤 Upload progress: {status.progress() * 100:.1f}%"
                    )
            except HttpError as e:
                code = getattr(e.resp, "status", 0)
                if code == 401:
                    raise AuthenticationError(
                        f"YouTube upload auth failed (HTTP 401)"
                    ) from e
                if code == 403 and "quotaExceeded" in str(e):
                    raise QuotaExceededError("YouTube quota exceeded") from e
                # 5xx → retry
                if code in (500, 502, 503, 504):
                    retries += 1
                    if retries > self._max_retries:
                        raise UploadError(
                            f"YouTube upload failed after {self._max_retries} "
                            f"retries: HTTP {code}",
                            video_path=request.video_path,
                            cause=e,
                        ) from e
                    backoff = min(2 ** retries, 60)
                    logger.warning(
                        f"⚠️ YouTube HTTP {code}; retry {retries}/"
                        f"{self._max_retries} in {backoff}s"
                    )
                    time.sleep(backoff)
                else:
                    raise UploadError(
                        f"YouTube upload failed: HTTP {code}: {e}",
                        video_path=request.video_path,
                        cause=e,
                    ) from e
            except requests.RequestException as e:
                retries += 1
                if retries > self._max_retries:
                    raise NetworkError(
                        f"YouTube upload network failure: {e}", cause=e
                    ) from e
                time.sleep(min(10 * retries, 60))

        video_id: str = response["id"]
        video_url = f"https://youtube.com/watch?v={video_id}"
        logger.info(f"✅ Uploaded: {video_url}")

        # ── Thumbnail
        thumb_uploaded = False
        if request.thumbnail_path and Path(request.thumbnail_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(
                        request.thumbnail_path,
                        mimetype="image/jpeg",
                    ),
                ).execute()
                thumb_uploaded = True
                logger.info("🖼️ Thumbnail uploaded")
            except Exception as e:
                # Non-fatal: video already uploaded
                logger.warning(f"⚠️ Thumbnail upload failed: {e}")

        return UploadResult(
            video_id=video_id,
            video_url=video_url,
            thumbnail_uploaded=thumb_uploaded,
        )
