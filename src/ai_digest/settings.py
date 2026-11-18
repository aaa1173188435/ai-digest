"""Runtime configuration.

Every field can be overridden by an environment variable of the same name
(case-insensitive). That is the single mechanism GitHub Actions uses to inject
secrets, so local runs and CI runs share one code path.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -- LLM ---------------------------------------------------------------
    llm_provider: str = "deepseek"  # "deepseek" | "fake"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_batch_size: int = 8
    llm_concurrency: int = 3
    llm_timeout: float = 90.0

    # -- storage / io ------------------------------------------------------
    database_path: Path = Path("data/digest.db")
    sources_path: Path = Path("config/sources.yaml")
    output_dir: Path = Path("public")
    eval_dir: Path = Path("eval")

    # -- ingest ------------------------------------------------------------
    fetch_timeout: float = 20.0
    fetch_concurrency: int = 6
    max_retries: int = 3
    per_source_limit: int = 10
    user_agent: str = "ai-digest/0.1"

    # -- ranking -----------------------------------------------------------
    top_n: int = 12
    prefilter_n: int = 30  # heuristic recall -> LLM precision; caps LLM spend
    max_per_source: int = 2  # keeps one prolific feed from owning the page
    near_dup_threshold: int = 3  # max hamming distance on the 64-bit simhash
    max_age_days: int = 4

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
