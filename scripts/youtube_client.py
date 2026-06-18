#!/usr/bin/env python3
"""Shared YouTube credential + service layer for Builds 3 (upload) and 4 (analytics).

Both ``upload_video.py`` and ``analytics_pull.py`` load the SAME long-lived
refresh token captured once by ``youtube_authorize.py`` and refresh it silently.
This module is the single place that:

  * loads ``youtube_token.json`` (authorized-user creds, repo root by default),
  * refreshes + persists a rotated refresh_token at 0o600,
  * builds the Data API v3 and Analytics API v2 service objects,
  * resolves the authorized channel's id + uploads-playlist id.

It deliberately holds NO upload/analytics logic — just auth + discovery — so the
two feature scripts share one battle-tested credential path. Nothing here ever
writes a credential into the vault (git-tracked + synced); the token lives beside
.env at the iris_studio repo root, which is .gitignored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root = parent of this scripts/ dir (mirrors youtube_authorize.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

# Must match youtube_authorize.py exactly. Credentials.from_authorized_user_file
# validates the on-disk token carries (at least) these scopes; a mismatch raises
# and we surface a clear re-authorize message rather than a cryptic 403 later.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

DEFAULT_TOKEN = os.environ.get("YOUTUBE_TOKEN", str(REPO_ROOT / "youtube_token.json"))


class YouTubeAuthError(RuntimeError):
    """Raised when credentials are missing, unreadable, or unrefreshable.

    Callers catch this to print a one-line remediation (run youtube_authorize.py)
    and exit non-zero, instead of leaking a google.auth traceback.
    """


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _save_token(creds, token_path: Path) -> None:
    """Persist the token JSON owner-only (0600), same contract as the authorizer.

    Google may rotate the refresh_token on a refresh, so the on-disk copy must be
    rewritten after every successful refresh or the unattended jobs eventually
    fail with an invalid_grant.
    """
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(creds.to_json())
    finally:
        os.chmod(token_path, 0o600)


def load_credentials(token_path: str | os.PathLike | None = None):
    """Return valid Credentials, refreshing + persisting if needed.

    Raises YouTubeAuthError when there is no token, it cannot be parsed, it lacks
    a refresh_token, or the refresh fails (revoked/expired). The unattended jobs
    never re-open a browser — re-consent is a human step (youtube_authorize.py).
    """
    from google.auth.transport.requests import Request
    from google.auth.exceptions import GoogleAuthError
    from google.oauth2.credentials import Credentials

    path = Path(token_path or DEFAULT_TOKEN).expanduser().resolve()
    if not path.exists():
        raise YouTubeAuthError(
            f"YouTube token not found: {path}. Run scripts/youtube_authorize.py "
            "once (interactively) to capture it."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, KeyError) as exc:
        raise YouTubeAuthError(
            f"Token at {path} is unreadable ({exc}); re-run youtube_authorize.py --force."
        ) from exc

    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except GoogleAuthError as exc:  # RefreshError (revoked/expired grant) et al.
            raise YouTubeAuthError(
                f"Could not refresh the YouTube token ({exc}); the grant may be "
                "revoked. Re-run youtube_authorize.py --force."
            ) from exc
        _save_token(creds, path)
        return creds
    # Valid==False and not refreshable (e.g. no refresh_token on the token file).
    raise YouTubeAuthError(
        f"Token at {path} is invalid and not refreshable (missing refresh_token). "
        "Re-run youtube_authorize.py --force."
    )


def build_data_service(creds):
    """YouTube Data API v3 service (uploads, captions, thumbnails, playlists)."""
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_analytics_service(creds):
    """YouTube Analytics API v2 service (CTR / AVD / retention reports)."""
    from googleapiclient.discovery import build

    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def resolve_channel(data_service) -> dict:
    """Return {id, title, uploads_playlist} for the authorized channel.

    Raises YouTubeAuthError if the token controls no channel (wrong account
    consented). uploads_playlist is the channel's auto-managed "all uploads"
    playlist — the canonical way to enumerate our own videos for analytics.
    """
    resp = (
        data_service.channels()
        .list(part="snippet,contentDetails", mine=True)
        .execute()
    )
    items = resp.get("items", [])
    if not items:
        raise YouTubeAuthError(
            "Authorized token controls no YouTube channel — re-authorize as the "
            "channel-owning account (youtube_authorize.py --force)."
        )
    ch = items[0]
    return {
        "id": ch["id"],
        "title": ch.get("snippet", {}).get("title", "(no title)"),
        "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
    }
