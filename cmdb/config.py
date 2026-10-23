"""Settings from the environment (or a .env file in the repo root, for runs outside Docker)."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    backend: str
    jira_site: str
    jira_email: str
    jira_api_token: str
    workspace_id: str
    api_base: str
    api_key: str = ""          # when set, /ci requests must send it
    cache_seconds: float = 60  # API response cache per CI; 0 disables

    @classmethod
    def from_env(cls) -> "Config":
        _load_dotenv()
        return cls(
            backend=os.getenv("CMDB_BACKEND", "jira").strip().lower(),
            jira_site=os.getenv("JIRA_SITE", "").strip().rstrip("/"),
            jira_email=os.getenv("JIRA_EMAIL", "").strip(),
            jira_api_token=os.getenv("JIRA_API_TOKEN", "").strip(),
            workspace_id=os.getenv("ASSETS_WORKSPACE_ID", "").strip(),
            api_base=os.getenv("ASSETS_API_BASE", "").strip().rstrip("/"),
            api_key=os.getenv("CMDB_API_KEY", "").strip(),
            cache_seconds=float(os.getenv("CMDB_CACHE_SECONDS", "60") or 0),
        )

    def missing_jira_settings(self) -> list[str]:
        needed = {"JIRA_EMAIL": self.jira_email, "JIRA_API_TOKEN": self.jira_api_token}
        if not self.api_base:
            needed["JIRA_SITE"] = self.jira_site
        return [name for name, value in needed.items() if not value]
