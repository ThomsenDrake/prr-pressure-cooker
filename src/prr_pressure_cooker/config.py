from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    db_path: Path = PROJECT_ROOT / "var" / "prr.db"
    casefiles_dir: Path = PROJECT_ROOT / "casefiles"
    deployment_name: str = "prr-pressure-cooker-dev"
    proton_himalaya_base_url: str | None = None
    himalaya_ssh_target: str | None = None
    himalaya_folder: str = "Folders/Public Records Requests"
    himalaya_account: str | None = None
    workflow_backend: str = "local"
    workflow_mode: str = "step"
    workflow_api_base_url: str | None = None
    requester_emails: tuple[str, ...] = ("drake.t98@proton.me", "drake@draket.xyz")

    @classmethod
    def from_env(cls) -> Settings:
        load_env_file()
        return cls(
            db_path=Path(os.getenv("PRR_DB_PATH", str(PROJECT_ROOT / "var" / "prr.db"))),
            casefiles_dir=Path(
                os.getenv("PRR_CASEFILES_DIR", str(PROJECT_ROOT / "casefiles"))
            ),
            deployment_name=os.getenv("DEPLOYMENT_NAME", "prr-pressure-cooker-dev"),
            proton_himalaya_base_url=os.getenv("PROTON_HIMALAYA_BASE_URL"),
            himalaya_ssh_target=os.getenv("HIMALAYA_SSH_TARGET"),
            himalaya_folder=os.getenv("HIMALAYA_FOLDER", "Folders/Public Records Requests"),
            himalaya_account=os.getenv("HIMALAYA_ACCOUNT"),
            workflow_backend=os.getenv("PRR_WORKFLOW_BACKEND", "local"),
            workflow_mode=os.getenv("PRR_WORKFLOW_MODE", "step"),
            workflow_api_base_url=os.getenv("PRR_WORKFLOW_API_BASE_URL"),
            requester_emails=tuple(
                email.strip().lower()
                for email in os.getenv(
                    "PRR_REQUESTER_EMAILS", "drake.t98@proton.me,drake@draket.xyz"
                ).split(",")
                if email.strip()
            ),
        )

    @property
    def mistral_api_key_present(self) -> bool:
        return bool(os.getenv("MISTRAL_API_KEY"))
