#!/usr/bin/env python3
"""One-time YouTube OAuth authorizer for 3SK Finance (Autonomy Roadmap Builds 3+4).

Steve runs this ONCE, interactively, on the Mac Mini, after enabling the YouTube
APIs + adding the scopes + downloading a Desktop OAuth client JSON from the
`iris-mcp-studio` Google Cloud project (the studio@ Internal app that already
backs the Drive/Gmail MCP). It:

  1. Opens the browser for the studio@ "Allow" consent (loopback flow).
  2. Captures the long-lived **refresh token** into ``youtube_token.json``.
  3. chmod 600s that token file (creds never leave the Mini; both the client
     secret and the token are .gitignored — never the repo, never the vault).
  4. Verifies the grant by naming the channel the token controls.

After this, the unattended Build-3 uploader and Build-4 analytics feed load
``youtube_token.json`` via ``Credentials.from_authorized_user_file`` and refresh
silently — no further human step until the refresh token is revoked.

Usage (from the iris_studio repo root, inside .venv):

    source .venv/bin/activate
    python scripts/youtube_authorize.py            # first run: opens browser
    python scripts/youtube_authorize.py            # later: reports existing grant
    python scripts/youtube_authorize.py --force     # re-consent from scratch

Idempotent: if a valid (or refreshable) token already exists, it reports the
channel and exits 0 without reopening the browser unless --force is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Repo root = parent of this scripts/ dir. Credential files default to living
# beside .env at the repo root (both are .gitignored).
REPO_ROOT = Path(__file__).resolve().parent.parent

# The scopes the roadmap needs. Order is irrelevant but kept stable so the
# token's recorded scope set is comparable run-to-run.
#   youtube.upload          -> Build 3: upload videos
#   youtube                 -> Build 3: set thumbnails, schedule publish
#   youtube.force-ssl       -> Build 3: write captions (captions.insert REQUIRES
#                              force-ssl; the plain `youtube` scope is not enough)
#   yt-analytics.readonly   -> Build 4: CTR / retention / AVD metrics
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

DEFAULT_CLIENT_SECRET = os.environ.get(
    "YOUTUBE_CLIENT_SECRET", str(REPO_ROOT / "client_secret_youtube.json")
)
DEFAULT_TOKEN = os.environ.get(
    "YOUTUBE_TOKEN", str(REPO_ROOT / "youtube_token.json")
)


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _load_existing(token_path: Path):
    """Return valid Credentials from token_path, refreshing if needed, else None.

    Returns None when there is no token file, when it cannot be parsed, or when a
    refresh is required but fails (revoked/expired refresh token) — the caller
    then falls through to a full browser re-auth.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, KeyError) as exc:
        _eprint(f"⚠️  Existing token at {token_path} is unreadable ({exc}); re-authorizing.")
        return None

    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist immediately: Google may rotate the refresh_token on refresh
            # for some grants, so the on-disk copy must not go stale. Written back
            # at 0o600 by _save_token.
            _save_token(creds, token_path)
            return creds
        except Exception as exc:  # google.auth.exceptions.RefreshError et al.
            _eprint(f"⚠️  Could not refresh existing token ({exc}); re-authorizing.")
            return None
    return None


def _save_token(creds, token_path: Path) -> None:
    """Write the token JSON and lock it to owner-only (0600) before AND after."""
    # Create the file with restrictive perms from the start so the refresh token
    # is never briefly world/group-readable on disk.
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(creds.to_json())
    finally:
        # Re-assert in case the file pre-existed with looser perms (O_CREAT keeps
        # the existing mode when the file already exists).
        os.chmod(token_path, 0o600)


def _verify_channel(creds) -> int:
    """Name the channel the token controls. Returns 0 on success, 1 on failure.

    A failure here does NOT invalidate the captured token — it usually means the
    YouTube Data API isn't enabled yet, or the account has no channel. We report
    and let the human decide; the token (with its refresh_token) is already saved.
    """
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            _eprint(
                "⚠️  Token is valid but the authorized account controls no YouTube "
                "channel. Make sure you consented as the channel-owning account."
            )
            return 1
        ch = items[0]
        title = ch.get("snippet", {}).get("title", "(no title)")
        cid = ch.get("id", "(no id)")
        print(f"✅ Authorized channel: {title}  (channelId={cid})")
        print(
            "   Record this channelId — Build 3 (upload) and Build 4 (analytics) "
            "will target it."
        )
        return 0
    except Exception as exc:  # HttpError, transport errors, etc.
        _eprint(
            "⚠️  Token saved, but the verification call failed: "
            f"{type(exc).__name__}: {exc}"
        )
        _eprint(
            "   Most likely the YouTube Data API v3 isn't enabled on the project "
            "yet, or quota is exhausted. The token itself is fine — re-run with "
            "--no-verify to skip this check, or enable the API and re-run."
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time YouTube OAuth authorizer (Builds 3+4).",
    )
    parser.add_argument(
        "--client-secret",
        default=DEFAULT_CLIENT_SECRET,
        help=f"Path to the downloaded Desktop OAuth client JSON "
        f"(default: {DEFAULT_CLIENT_SECRET}).",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help=f"Where to write/read the authorized-user token "
        f"(default: {DEFAULT_TOKEN}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore any existing token and run the full browser consent again.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-auth channels().list verification call.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback port for the OAuth redirect (default: 0 = pick a free "
        "port; Desktop OAuth clients accept any loopback port).",
    )
    args = parser.parse_args(argv)

    token_path = Path(args.token).expanduser().resolve()
    client_secret_path = Path(args.client_secret).expanduser().resolve()

    # 1) Idempotent fast-path: reuse a still-good token unless --force.
    if not args.force:
        creds = _load_existing(token_path)
        if creds is not None:
            print(f"✓ Existing YouTube token is valid: {token_path}")
            if args.no_verify:
                return 0
            return _verify_channel(creds)

    # 2) Full consent flow — needs the client secret.
    if not client_secret_path.exists():
        _eprint(f"❌ Client secret not found: {client_secret_path}")
        _eprint(
            "\nDownload it first: Google Cloud console → project `iris-mcp-studio` "
            "→ APIs & Services → Credentials → Create credentials → OAuth client "
            "ID → Application type **Desktop app** → Download JSON, and save it to "
            "the path above (or pass --client-secret <path>). It's .gitignored."
        )
        return 2

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    print(
        "Opening a browser for the studio@ consent. Sign in as the account that "
        "owns the 3SK Finance channel and click Allow…"
    )
    # access_type=offline + prompt=consent GUARANTEE a refresh_token is returned,
    # even on a repeat consent for an already-granted app (otherwise Google may
    # omit it and the unattended jobs would have no way to refresh).
    creds = flow.run_local_server(
        port=args.port,
        access_type="offline",
        prompt="consent",
        success_message=(
            "YouTube authorization complete — you can close this tab and return "
            "to the terminal."
        ),
    )

    if not creds.refresh_token:
        _eprint(
            "❌ Consent succeeded but NO refresh_token was returned, so unattended "
            "refresh would be impossible. Re-run with --force to force a fresh "
            "consent prompt."
        )
        return 3

    _save_token(creds, token_path)
    print(f"✅ Saved refresh token → {token_path} (chmod 600)")

    # The unbundled consent screen lets the user UNCHECK individual scopes, so a
    # "successful" consent can still mint a token missing the one we need most
    # (force-ssl = caption writes). Assert it landed; a silent miss => later 403.
    # Check GRANTED scopes (creds.granted_scopes = what the user actually consented
    # to), NOT creds.scopes (what the flow REQUESTED — always our full SCOPES list,
    # so it would never catch an unchecked box). No fallback to .scopes: if the token
    # endpoint omits the granted set we'd rather warn (safe-side false positive) than
    # silently miss a real gap.
    ssl_scope = "https://www.googleapis.com/auth/youtube.force-ssl"
    granted = set(getattr(creds, "granted_scopes", None) or [])
    if ssl_scope not in granted:
        _eprint(
            "⚠️  WARNING: the minted token does NOT carry youtube.force-ssl — "
            "caption uploads (captions.insert) WILL 403. You likely unchecked it "
            "on the consent screen. Re-run with --force and leave every box "
            f"checked. Granted scopes: {sorted(granted) or 'unknown'}"
        )

    if args.no_verify:
        return 0
    return _verify_channel(creds)


if __name__ == "__main__":
    raise SystemExit(main())
