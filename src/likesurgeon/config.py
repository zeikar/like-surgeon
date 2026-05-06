"""Local app configuration: paths, env overrides, on-disk layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_DIR_NAME = ".like-surgeon"
DEFAULT_DB_FILENAME = "like-surgeon.sqlite"
YTMUSIC_BROWSER_FILENAME = "browser.json"
YOUTUBE_OAUTH_CLIENT_FILENAME = "youtube-oauth-client.json"
YOUTUBE_TOKEN_FILENAME = "youtube-token.json"
ENV_HOME = "LIKE_SURGEON_HOME"


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
            youtube_oauth_client_path=app_dir / YOUTUBE_OAUTH_CLIENT_FILENAME,
            youtube_token_path=app_dir / YOUTUBE_TOKEN_FILENAME,
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
