"""
infrastructure/asset_storage.py — VALUE / QEEMA v22.7 (NEW)
=========================================================================
Persistent asset storage via Supabase Storage.

[Why this exists]
The 3-day phase pipeline runs Phase 2 and Phase 3 on separate GitHub Actions
runners on different days. Each runner gets a fresh VM — temp/ is wiped.

Until v22.7, Phase 2 generated audio/image files into temp/episodes/episode_NNN/
and Phase 3 expected to find them there. On a fresh runner, those files are
gone and Phase 3 crashed with "Audio missing for render".

This module bridges the gap by mirroring the episode's local temp directory
to a Supabase Storage bucket. Phase 2 uploads, Phase 3 downloads. The
phase state (state/phases/episode_NNN.json — already cached by GitHub Actions)
carries a *manifest* dict mapping storage keys → relative file paths, so
Phase 3 knows exactly which keys to fetch.

[Design decisions]
1. Manifest-driven, not list-based.
   We do NOT rely on Supabase Storage's list() API for hydration. The manifest
   in phase state is the source of truth. This avoids pagination edge cases
   and "is-this-a-folder-or-a-file" heuristics.

2. Fail loud on partial upload.
   If ANY file fails to upload during Phase 2, we raise. Better to fail
   Phase 2 today and retry tomorrow than to pretend success and have
   Phase 3 fall apart on day three.

3. Parallel I/O with conservative concurrency.
   Upload uses 4 workers, download uses 6. Supabase Storage tolerates this
   easily; higher parallelism risks rate-limit spikes for no real speed gain
   on a ~30-file episode.

4. Replace-on-upload semantics.
   `upload_episode_dir(replace=True)` (default) deletes the storage prefix
   first. Prevents leftover files from earlier failed attempts from leaking
   into the new manifest.

[Bucket]
We use a single bucket `episode-artifacts`. Layout:
    episode-artifacts/
      episode_001/
        intro_narrator.mp3
        outro_narrator.mp3
        ayah_1_hook.mp3
        ...
        mastered/
          intro_narrator.m4a
          outro_narrator.m4a
          ...
        ai_images/
          ayah_1.png
          ...
      episode_002/
        ...

The first time this code runs, the bucket is created (private). If your
Supabase service-role token can't create buckets, create it manually in the
dashboard and the code will continue silently.

[Storage cost expectations]
A typical episode has:
  - ~30 audio files (mp3 + m4a) → ~10-20 MB
  - 7 AI images (PNG) → ~10-20 MB
Total per episode: ~40 MB. Supabase free tier is 1 GB → ~25 episodes.
Add a cleanup pass for episodes already on YouTube to recover space.

[Public API]
    storage = AssetStorage(supabase_client)
    manifest = storage.upload_episode_dir(1, "/path/to/temp/episodes/episode_001")
    # ... store manifest in phase state ...
    # later, on a different runner:
    storage.download_from_manifest(1, manifest, "/path/to/temp/episodes/episode_001")

If the Supabase client isn't available (e.g. --skip-supabase), don't instantiate
this class. Callers should treat `None` as "no persistence available".
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_BUCKET: str = "episode-artifacts"
UPLOAD_PARALLELISM: int = 4
DOWNLOAD_PARALLELISM: int = 6
RETRY_ATTEMPTS: int = 3
RETRY_BACKOFF_S: float = 2.0


class AssetStorageError(RuntimeError):
    """Raised when an unrecoverable storage operation fails."""


# ════════════════════════════════════════════════════════════════
# MIME types — set explicitly on upload so the Supabase Storage UI
# previews files correctly and signed URLs return the right headers.
# ════════════════════════════════════════════════════════════════
_MIME_BY_EXT: Dict[str, str] = {
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
    ".wav":  "audio/wav",
    ".flac": "audio/flac",
    ".ogg":  "audio/ogg",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".mp4":  "video/mp4",
    ".mov":  "video/quicktime",
    ".webm": "video/webm",
    ".json": "application/json",
    ".txt":  "text/plain",
    ".md":   "text/markdown",
    ".ass":  "text/plain",
    ".srt":  "text/plain",
    ".vtt":  "text/vtt",
}


class AssetStorage:
    """Upload/download episode asset directories to/from Supabase Storage."""

    def __init__(self, supabase_client: Any, bucket: str = DEFAULT_BUCKET) -> None:
        if supabase_client is None:
            raise ValueError(
                "AssetStorage: supabase_client is required. "
                "Skip wiring AssetStorage entirely if Supabase is disabled."
            )
        self.supabase = supabase_client
        self.bucket = bucket
        self._ensure_bucket()

    # ──────────────────────────────────────────────────────────────────────
    # Bucket management
    # ──────────────────────────────────────────────────────────────────────
    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't already exist. Idempotent.

        We don't fail hard if the bucket-list check fails — some Supabase
        tokens lack list-buckets permission but can still upload/download
        if the bucket already exists. Log and continue.
        """
        try:
            buckets = self.supabase.storage.list_buckets() or []
            names = set()
            for b in buckets:
                name = getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None)
                if name:
                    names.add(name)
            if self.bucket in names:
                logger.info(f"📦 AssetStorage: bucket '{self.bucket}' ready")
                return
            try:
                self.supabase.storage.create_bucket(
                    self.bucket,
                    options={"public": False},
                )
                logger.info(f"📦 AssetStorage: created private bucket '{self.bucket}'")
            except Exception as create_err:
                # Some Supabase tokens can list but can't create. If the
                # bucket already exists (race), we'll discover that on first
                # upload. Log and continue.
                logger.warning(
                    f"⚠️ AssetStorage: could not create bucket '{self.bucket}' "
                    f"({create_err!r}). If it already exists, ignore this. "
                    f"Otherwise create it manually in the Supabase dashboard."
                )
        except Exception as e:
            logger.warning(
                f"⚠️ AssetStorage: bucket check inconclusive ({e!r}); "
                f"assuming bucket '{self.bucket}' exists."
            )

    # ──────────────────────────────────────────────────────────────────────
    # Single-file ops (with retry)
    # ──────────────────────────────────────────────────────────────────────
    def upload_file(self, local_path: str | Path, storage_key: str) -> str:
        """Upload one local file. Overwrites if key exists. Returns the key."""
        local = Path(local_path)
        if not local.is_file():
            raise AssetStorageError(f"upload_file: missing local file: {local_path}")
        with open(local, "rb") as fh:
            data = fh.read()

        last_err: Optional[Exception] = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                self.supabase.storage.from_(self.bucket).upload(
                    path=storage_key,
                    file=data,
                    file_options={
                        "upsert": "true",
                        "content-type": self._guess_mime(local),
                    },
                )
                return storage_key
            except Exception as e:
                last_err = e
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_BACKOFF_S * (2 ** (attempt - 1))
                    logger.warning(
                        f"⚠️ upload_file: attempt {attempt}/{RETRY_ATTEMPTS} failed "
                        f"for {storage_key} ({type(e).__name__}: {e}); "
                        f"retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
        raise AssetStorageError(
            f"upload_file: exhausted {RETRY_ATTEMPTS} attempts for {storage_key}: "
            f"{type(last_err).__name__}: {last_err}"
        )

    def download_file(self, storage_key: str, local_path: str | Path) -> str:
        """Download one file by storage key. Creates parent dirs as needed."""
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)

        last_err: Optional[Exception] = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                data = self.supabase.storage.from_(self.bucket).download(storage_key)
                with open(local, "wb") as fh:
                    fh.write(data)
                return str(local)
            except Exception as e:
                last_err = e
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_BACKOFF_S * (2 ** (attempt - 1))
                    logger.warning(
                        f"⚠️ download_file: attempt {attempt}/{RETRY_ATTEMPTS} failed "
                        f"for {storage_key} ({type(e).__name__}: {e}); "
                        f"retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
        raise AssetStorageError(
            f"download_file: exhausted {RETRY_ATTEMPTS} attempts for {storage_key}: "
            f"{type(last_err).__name__}: {last_err}"
        )

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under a prefix. Used for episode cleanup. Best-effort."""
        try:
            keys = self._list_prefix_recursive(prefix)
            if not keys:
                return 0
            self.supabase.storage.from_(self.bucket).remove(keys)
            logger.info(f"🗑️ AssetStorage: deleted {len(keys)} files under {prefix}/")
            return len(keys)
        except Exception as e:
            logger.warning(f"⚠️ delete_prefix({prefix}) failed: {e!r}")
            return 0

    # ──────────────────────────────────────────────────────────────────────
    # Episode-level ops (the main API)
    # ──────────────────────────────────────────────────────────────────────
    def upload_episode_dir(
        self,
        episode_number: int,
        local_dir: str | Path,
        *,
        replace: bool = True,
    ) -> Dict[str, str]:
        """Recursively upload everything under local_dir to Supabase Storage.

        Args:
            episode_number: 1-based episode number.
            local_dir: Local directory whose contents will be mirrored.
            replace: If True (default), delete the storage prefix first so
                     leftover files from earlier failed attempts don't linger.

        Returns:
            Manifest dict {storage_key: rel_path_inside_local_dir}.
            Persist this in phase state so Phase 3 can rehydrate.

        Raises:
            AssetStorageError on any failed upload (NOT silent — better to
            fail Phase 2 than to corrupt Phase 3).
        """
        local_root = Path(local_dir)
        if not local_root.is_dir():
            logger.warning(f"⚠️ upload_episode_dir: local dir missing: {local_dir}")
            return {}

        prefix = self._episode_prefix(episode_number)

        if replace:
            self.delete_prefix(prefix)

        files = [p for p in local_root.rglob("*") if p.is_file()]
        if not files:
            logger.warning(f"⚠️ upload_episode_dir: no files in {local_dir}")
            return {}

        logger.info(
            f"☁️ AssetStorage: uploading {len(files)} files to "
            f"{self.bucket}/{prefix}/ (parallelism={UPLOAD_PARALLELISM})"
        )

        manifest: Dict[str, str] = {}
        failures: List[Tuple[Path, Exception]] = []
        t0 = time.time()

        def _upload_one(fp: Path) -> Tuple[str, str]:
            rel = fp.relative_to(local_root).as_posix()
            key = f"{prefix}/{rel}"
            self.upload_file(fp, key)
            return key, rel

        with ThreadPoolExecutor(max_workers=UPLOAD_PARALLELISM) as pool:
            futures = {pool.submit(_upload_one, fp): fp for fp in files}
            for fut in as_completed(futures):
                fp = futures[fut]
                try:
                    key, rel = fut.result()
                    manifest[key] = rel
                except Exception as e:
                    failures.append((fp, e))

        elapsed = time.time() - t0
        logger.info(
            f"☁️ AssetStorage: uploaded {len(manifest)}/{len(files)} in {elapsed:.1f}s"
        )
        if failures:
            for fp, e in failures[:5]:
                logger.error(f"   ✗ {fp.name}: {type(e).__name__}: {e}")
            if len(failures) > 5:
                logger.error(f"   ... and {len(failures) - 5} more failures")
            raise AssetStorageError(
                f"upload_episode_dir: {len(failures)} files failed for "
                f"episode {episode_number}. Aborting to prevent silent "
                f"Phase 3 corruption. Retry Phase 2 to recover."
            )
        return manifest

    def download_from_manifest(
        self,
        episode_number: int,
        manifest: Dict[str, str],
        local_dir: str | Path,
    ) -> int:
        """Download every file in the manifest into local_dir.

        The original directory layout is reconstructed from the manifest's
        rel_path values. So /local_dir/mastered/intro.m4a is restored from
        storage key `episode_001/mastered/intro.m4a` whose rel is
        `mastered/intro.m4a`.

        Args:
            episode_number: 1-based episode number (used only for logging).
            manifest: {storage_key: rel_path} from upload_episode_dir.
            local_dir: Target directory. Created if missing.

        Returns:
            Count of files successfully downloaded.

        Raises:
            AssetStorageError if any required file is missing — Phase 3 has
            no way to proceed without all its inputs.
        """
        if not manifest:
            logger.warning(
                f"⚠️ download_from_manifest: empty manifest for episode {episode_number}"
            )
            return 0

        local_root = Path(local_dir)
        local_root.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"☁️ AssetStorage: downloading {len(manifest)} files from "
            f"{self.bucket}/episode_{episode_number:03d}/ → {local_root} "
            f"(parallelism={DOWNLOAD_PARALLELISM})"
        )

        failures: List[Tuple[str, Exception]] = []
        success_count = 0
        t0 = time.time()

        def _dl_one(item: Tuple[str, str]) -> str:
            storage_key, rel_path = item
            target = local_root / rel_path
            self.download_file(storage_key, target)
            return storage_key

        with ThreadPoolExecutor(max_workers=DOWNLOAD_PARALLELISM) as pool:
            futures = {pool.submit(_dl_one, item): item for item in manifest.items()}
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    fut.result()
                    success_count += 1
                except Exception as e:
                    failures.append((item[0], e))

        elapsed = time.time() - t0
        logger.info(
            f"☁️ AssetStorage: downloaded {success_count}/{len(manifest)} "
            f"in {elapsed:.1f}s"
        )
        if failures:
            for key, e in failures[:5]:
                logger.error(f"   ✗ {key}: {type(e).__name__}: {e}")
            if len(failures) > 5:
                logger.error(f"   ... and {len(failures) - 5} more failures")
            raise AssetStorageError(
                f"download_from_manifest: {len(failures)} files failed for "
                f"episode {episode_number}. Phase 3 cannot proceed safely."
            )
        return success_count

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _episode_prefix(episode_number: int) -> str:
        return f"episode_{episode_number:03d}"

    def _list_prefix_recursive(self, prefix: str) -> List[str]:
        """List every storage key under a prefix. Used only for cleanup."""
        results: List[str] = []
        stack = [prefix]
        while stack:
            current = stack.pop()
            try:
                items = self.supabase.storage.from_(self.bucket).list(current) or []
            except Exception as e:
                logger.warning(f"⚠️ list({current}) failed: {e!r}")
                continue
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name")
                    is_folder = item.get("id") is None and item.get("metadata") is None
                else:
                    name = getattr(item, "name", None)
                    is_folder = (
                        getattr(item, "id", None) is None
                        and getattr(item, "metadata", None) is None
                    )
                if not name:
                    continue
                full = f"{current}/{name}" if current else name
                if is_folder:
                    stack.append(full)
                else:
                    results.append(full)
        return results

    @staticmethod
    def _guess_mime(p: Path) -> str:
        return _MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")
