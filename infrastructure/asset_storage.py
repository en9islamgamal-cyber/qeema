"""
QEEMA v22.7 — Persistent Asset Storage

Bridges the ephemeral GitHub Actions runner filesystem with Supabase Storage,
so Phase 2 can write assets in one runner and Phase 3 can read them in another.

Design:
  - Each episode's local `temp/episodes/episode_NNN/` directory is mirrored to
    Supabase Storage under the prefix `episode_NNN/`.
  - Upload returns a *manifest* dict {storage_key: relative_path_inside_ep_dir}.
    The manifest is persisted in the phase state (asset_paths["_storage_manifest"]).
  - Phase 3 hydrates by reading the manifest and downloading each key into
    the new runner's temp directory.
  - We deliberately do NOT rely on Supabase Storage's `list()` API for hydration —
    the manifest is the source of truth. This avoids pagination, eventual-consistency,
    and "is-this-a-folder-or-file" heuristics.

Usage:
    storage = AssetStorage(supabase_client)
    manifest = storage.upload_episode_dir(1, "/tmp/episodes/episode_001")
    # ... store manifest in phase state ...
    # later, on a different runner:
    storage.download_from_manifest(1, manifest, "/tmp/episodes/episode_001")
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "episode-artifacts"
UPLOAD_PARALLELISM = 6
DOWNLOAD_PARALLELISM = 8
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0


class AssetStorageError(RuntimeError):
    """Raised when an unrecoverable storage operation fails."""


class AssetStorage:
    """Upload/download episode asset directories to/from Supabase Storage.

    The bucket layout is:
        episode-artifacts/
          episode_001/
            intro_narrator.mp3
            mastered/intro_narrator.m4a
            mastered/ayah_1_hook.m4a
            ai_images/ayah_1.png
            ...
          episode_002/
            ...
    """

    def __init__(self, supabase_client: Any, bucket: str = DEFAULT_BUCKET) -> None:
        if supabase_client is None:
            raise ValueError("supabase_client is required (skip wiring if Supabase disabled)")
        self.supabase = supabase_client
        self.bucket = bucket
        self._ensure_bucket()

    # ──────────────────────────────────────────────────────────────────────
    # Bucket management
    # ──────────────────────────────────────────────────────────────────────

    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't already exist. Idempotent."""
        try:
            buckets = self.supabase.storage.list_buckets()
            names = {getattr(b, "name", None) or b.get("name") for b in buckets}  # type: ignore[union-attr]
            if self.bucket in names:
                logger.info(f"📦 AssetStorage: bucket '{self.bucket}' ready")
                return
            self.supabase.storage.create_bucket(
                self.bucket,
                options={"public": False},
            )
            logger.info(f"📦 AssetStorage: created bucket '{self.bucket}'")
        except Exception as e:
            # Don't hard-fail on bucket check — Supabase auth tokens sometimes lack
            # bucket-list permission but can still upload/download. Log and continue.
            logger.warning(f"⚠️ AssetStorage: bucket check inconclusive ({e!r}); assuming exists")

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

        last_err: Exception | None = None
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
                        f"for {storage_key} ({e!r}); retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
        raise AssetStorageError(
            f"upload_file: exhausted {RETRY_ATTEMPTS} attempts for {storage_key}: {last_err!r}"
        )

    def download_file(self, storage_key: str, local_path: str | Path) -> str:
        """Download one file by storage key. Creates parent dirs as needed."""
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)

        last_err: Exception | None = None
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
                        f"for {storage_key} ({e!r}); retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
        raise AssetStorageError(
            f"download_file: exhausted {RETRY_ATTEMPTS} attempts for {storage_key}: {last_err!r}"
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
    ) -> dict[str, str]:
        """Recursively upload everything under local_dir.

        Returns a manifest dict: {storage_key: rel_path_inside_ep_dir}.
        The manifest must be persisted in phase state so Phase 3 can rehydrate.

        If `replace=True` (default), the entire prior contents of the storage prefix
        are deleted first so leftover files from earlier attempts don't linger.
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

        manifest: dict[str, str] = {}
        failures: list[tuple[Path, Exception]] = []
        t0 = time.time()

        def _upload_one(fp: Path) -> tuple[Path, str]:
            rel = fp.relative_to(local_root).as_posix()
            key = f"{prefix}/{rel}"
            self.upload_file(fp, key)
            return fp, key

        with ThreadPoolExecutor(max_workers=UPLOAD_PARALLELISM) as pool:
            futures = {pool.submit(_upload_one, fp): fp for fp in files}
            for fut in as_completed(futures):
                fp = futures[fut]
                try:
                    _, key = fut.result()
                    rel = fp.relative_to(local_root).as_posix()
                    manifest[key] = rel
                except Exception as e:
                    failures.append((fp, e))

        elapsed = time.time() - t0
        logger.info(
            f"☁️ AssetStorage: uploaded {len(manifest)}/{len(files)} in {elapsed:.1f}s"
        )
        if failures:
            for fp, e in failures[:5]:
                logger.error(f"   ✗ {fp.name}: {e!r}")
            if len(failures) > 5:
                logger.error(f"   ... and {len(failures) - 5} more failures")
            # Fail loudly — a partial upload will silently break Phase 3 later.
            raise AssetStorageError(
                f"upload_episode_dir: {len(failures)} files failed to upload for "
                f"episode {episode_number}; aborting to avoid silent Phase 3 corruption."
            )
        return manifest

    def download_from_manifest(
        self,
        episode_number: int,
        manifest: dict[str, str],
        local_dir: str | Path,
    ) -> int:
        """Download every file in the manifest into local_dir, preserving structure.

        Returns the count of files successfully downloaded.
        Raises AssetStorageError if any required file is missing.
        """
        if not manifest:
            logger.warning(f"⚠️ download_from_manifest: empty manifest for episode {episode_number}")
            return 0

        local_root = Path(local_dir)
        local_root.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"☁️ AssetStorage: downloading {len(manifest)} files from "
            f"{self.bucket}/episode_{episode_number:03d}/ → {local_root} "
            f"(parallelism={DOWNLOAD_PARALLELISM})"
        )

        failures: list[tuple[str, Exception]] = []
        success_count = 0
        t0 = time.time()

        def _dl_one(item: tuple[str, str]) -> str:
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
            f"☁️ AssetStorage: downloaded {success_count}/{len(manifest)} in {elapsed:.1f}s"
        )
        if failures:
            for key, e in failures[:5]:
                logger.error(f"   ✗ {key}: {e!r}")
            if len(failures) > 5:
                logger.error(f"   ... and {len(failures) - 5} more failures")
            raise AssetStorageError(
                f"download_from_manifest: {len(failures)} files failed to download for "
                f"episode {episode_number}; Phase 3 cannot proceed safely."
            )
        return success_count

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _episode_prefix(episode_number: int) -> str:
        return f"episode_{episode_number:03d}"

    def _list_prefix_recursive(self, prefix: str) -> list[str]:
        """List every storage key under a prefix. Used only for cleanup deletion."""
        results: list[str] = []
        stack = [prefix]
        while stack:
            current = stack.pop()
            try:
                items = self.supabase.storage.from_(self.bucket).list(current) or []
            except Exception as e:
                logger.warning(f"⚠️ list({current}) failed: {e!r}")
                continue
            for item in items:
                name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
                if not name:
                    continue
                full = f"{current}/{name}" if current else name
                # Supabase indicates folders via metadata == None and id == None.
                is_folder = (
                    isinstance(item, dict)
                    and item.get("id") is None
                    and item.get("metadata") is None
                )
                if is_folder:
                    stack.append(full)
                else:
                    results.append(full)
        return results

    @staticmethod
    def _guess_mime(p: Path) -> str:
        return _MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")


_MIME_BY_EXT: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".ass": "text/plain",
    ".srt": "text/plain",
    ".vtt": "text/vtt",
}
