"""get_token.py — VALUE / QEEMA v2 — YouTube OAuth"""
import os, json, logging, requests, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from config import APIKeys

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload","https://www.googleapis.com/auth/youtube"]
AUTH_URL   = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL  = "https://oauth2.googleapis.com/token"
REDIRECT   = "http://localhost:8080"

class _Handler(BaseHTTPRequestHandler):
    code = None
    def do_GET(self):
        p = parse_qs(urlparse(self.path).query)
        if "code" in p:
            _Handler.code = p["code"][0]
            self.send_response(200); self.end_headers()
            self.wfile.write("<h2>تم! أغلق النافذة</h2>".encode())
    def log_message(self, *a): pass

class YouTubeTokenManager:
    def __init__(self):
        self.client_id     = APIKeys.YT_CLIENT_ID
        self.client_secret = APIKeys.YT_CLIENT_SECRET
        self.refresh_token = APIKeys.YT_REFRESH_TOKEN

    def get_valid_access_token(self) -> str:
        if not self.refresh_token:
            raise ValueError("YOUTUBE_REFRESH_TOKEN غير موجود")
        r = requests.post(TOKEN_URL, data={
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type":    "refresh_token",
        }, timeout=15)
        r.raise_for_status()
        t = r.json().get("access_token")
        if not t: raise ValueError(f"فشل تجديد الرمز: {r.json()}")
        logger.info("✅ تم تجديد access_token")
        return t

    def first_time_auth(self) -> dict:
        params = "&".join([
            f"client_id={self.client_id}",
            f"redirect_uri={REDIRECT}",
            "response_type=code",
            f"scope={'%20'.join(SCOPES)}",
            "access_type=offline",
            "prompt=consent",
        ])
        url = f"{AUTH_URL}?{params}"
        logger.info(f"🌐 افتح: {url}")
        try: webbrowser.open(url)
        except: pass
        srv = HTTPServer(("localhost", 8080), _Handler)
        srv.timeout = 180; srv.handle_request()
        if not _Handler.code: raise Exception("لم يُستلم auth code")
        tokens = requests.post(TOKEN_URL, data={
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "code":          _Handler.code,
            "grant_type":    "authorization_code",
            "redirect_uri":  REDIRECT,
        }, timeout=15).json()
        rt = tokens.get("refresh_token")
        if rt:
            logger.info(f"\n{'='*60}\nYOUTUBE_REFRESH_TOKEN = {rt}\n{'='*60}")
        Path("youtube_token.json").write_text(json.dumps(tokens, indent=2))
        return tokens

if __name__ == "__main__":
    m = YouTubeTokenManager()
    c = input("1=أول مرة، 2=تجديد: ").strip()
    if c == "1": m.first_time_auth()
    else: print(m.get_valid_access_token()[:30]+"…")
