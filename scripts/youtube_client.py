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

# Must match youtube_authorize.py exactly. NOTE: from_authorized_user_file does
# NOT validate granted-vs-requested scopes and does not raise on mismatch — the
# list passed here just becomes creds.scopes. A scope the on-disk token was never
# granted surfaces only at REFRESH time (invalid_scope -> RefreshError ->
# YouTubeAuthError), never at load. So after WIDENING this list, re-run
# youtube_authorize.py --force promptly: the old token keeps working until its
# access token expires, after which the next refresh may fail until re-consent.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
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


# Re-exec sentinel: set on the child after we hand off to the venv interpreter, so
# a still-missing dep there fails loudly instead of looping execve forever.
_VENV_REEXEC_ENV = "IRIS_VENV_REEXEC"
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _ensure_runtime_deps() -> None:
    """Guarantee the Google API deps are importable, self-correcting if they aren't.

    The google-* packages only live in the iris_studio venv (``REPO_ROOT/.venv``).
    The launchd jobs run under it (run_iris.sh sources the venv), but a MANUAL CLI
    invocation with the macOS system python (``python3 scripts/upload_video.py``)
    has no google-auth and used to crash with ModuleNotFoundError deep inside
    ``load_credentials`` (it bit a real V4 upload). Every feature script imports
    this module at top level, so doing the check here once heals all of them.

    Behavior:
      * deps already importable  -> no-op (the venv / launchd happy path).
      * deps missing, venv python exists, not yet re-exec'd -> re-exec the SAME
        argv under the venv interpreter (transparent self-correction).
      * deps missing and we ARE the re-exec'd child -> the venv is broken; fail
        loudly with a pip remediation rather than execve-loop.
      * deps missing and no venv python on disk -> fail loudly naming the path.
    """
    try:
        import google.auth  # noqa: F401  cheap probe for the API dep set.
        import googleapiclient.discovery  # noqa: F401  catch a partial install too.
        return
    except ImportError:
        pass

    if os.environ.get(_VENV_REEXEC_ENV) == "1":
        _eprint(
            f"error: Google API deps still missing after switching to {_VENV_PYTHON}. "
            f"The venv is incomplete — run: {_VENV_PYTHON} -m pip install -r "
            f"{REPO_ROOT / 'requirements.txt'}"
        )
        raise SystemExit(3)

    if not _VENV_PYTHON.exists():
        _eprint(
            f"error: Google API deps (google-auth, google-api-python-client) are not "
            f"installed for this interpreter ({sys.executable}), and no venv was found "
            f"at {_VENV_PYTHON}. Run this script with the iris_studio venv python, e.g. "
            f"{REPO_ROOT / '.venv/bin/python'} {' '.join(sys.argv) or 'scripts/...'}"
        )
        raise SystemExit(3)

    # Hand off: replace this process with the venv interpreter running the same
    # argv. argv[0] is the entry script (e.g. scripts/upload_video.py) because
    # this module is imported by it, so the re-exec re-runs the user's command.
    try:
        os.execve(
            str(_VENV_PYTHON),
            [str(_VENV_PYTHON), *sys.argv],
            {**os.environ, _VENV_REEXEC_ENV: "1"},
        )
    except OSError as exc:  # dangling symlink, wrong-arch python, not executable…
        _eprint(
            f"error: could not launch the venv interpreter {_VENV_PYTHON} ({exc}). "
            f"The venv looks corrupt — recreate it, then run: {_VENV_PYTHON} -m pip "
            f"install -r {REPO_ROOT / 'requirements.txt'}"
        )
        raise SystemExit(3)


_ensure_runtime_deps()


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


def build_reporting_service(creds):
    """YouTube Reporting API v1 service (bulk pre-generated CSV reports).

    This is the ONLY API surface that serves thumbnail impressions + impression
    CTR per video (``video_thumbnail_impressions`` / ``video_thumbnail_impressions_ctr``,
    added by Google 2026-01-15). The real-time Analytics API v2 (build_analytics_service)
    recognizes the equivalent identifiers but rejects them in ``reports().query()`` —
    they are bulk-only. Reach those metrics via reporting jobs (see reporting_reach.py).
    """
    from googleapiclient.discovery import build

    return build("youtubereporting", "v1", credentials=creds, cache_discovery=False)


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
