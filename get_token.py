"""
get_token.py — VALUE / QEEMA v5.0
===================================
YouTube OAuth Token Manager.

⚠️ ملاحظة هامة: الـ orchestrator القديم كان بيستورد من هنا لكن الملف غير موجود!
ده كان السبب الثاني لفشل مرحلة الـ upload.

يستخدم refresh_token المحفوظ في GitHub Secrets للحصول على access_token جديد
كل ما لزم الأمر (التوكن صالح لـ 60 دقيقة عادة).
"""

import os
import time
import logging
import requests

logger = logging.getLogger(__name__)


class YouTubeTokenManager:
    """مدير توكنز يوتيوب — يستبدل refresh_token بـ access_token عند الحاجة."""

    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self):
        self.client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
        self.client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
        self.refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

        if not all([self.client_id, self.client_secret, self.refresh_token]):
            missing = []
            if not self.client_id: missing.append("YOUTUBE_CLIENT_ID")
            if not self.client_secret: missing.append("YOUTUBE_CLIENT_SECRET")
            if not self.refresh_token: missing.append("YOUTUBE_REFRESH_TOKEN")
            raise RuntimeError(f"❌ Missing YouTube credentials: {missing}")

        self._cached_token: str = ""
        self._expires_at: float = 0.0

    def get_valid_access_token(self) -> str:
        """رجّع توكن صالح (يُجدّد لو قارب على الانتهاء)."""
        # 60 ثانية buffer قبل الانتهاء
        if self._cached_token and time.time() < (self._expires_at - 60):
            return self._cached_token

        logger.info("🔄 تجديد YouTube access token...")
        resp = requests.post(
            self.OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"❌ فشل تجديد التوكن: HTTP {resp.status_code}\n"
                f"الرد: {resp.text[:300]}\n"
                f"تأكد إن YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN صحيحين."
            )

        data = resp.json()
        self._cached_token = data["access_token"]
        # expires_in is in seconds; usually 3600
        self._expires_at = time.time() + int(data.get("expires_in", 3600))

        logger.info(f"✅ تم تجديد التوكن (يصلح لـ {data.get('expires_in', 3600)}s)")
        return self._cached_token


# Convenience function
_singleton: YouTubeTokenManager = None


def get_youtube_credentials():
    """رجّع google.oauth2.credentials.Credentials جاهز للاستخدام."""
    global _singleton
    if _singleton is None:
        _singleton = YouTubeTokenManager()

    import google.oauth2.credentials
    token = _singleton.get_valid_access_token()
    return google.oauth2.credentials.Credentials(token=token)


if __name__ == "__main__":
    # Test mode
    logging.basicConfig(level=logging.INFO)
    mgr = YouTubeTokenManager()
    tok = mgr.get_valid_access_token()
    print(f"✅ Got token (length: {len(tok)})")
