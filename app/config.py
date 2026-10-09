"""Runtime configuration, read from environment variables only."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse

SANDBOX_BASE_URL = "https://api.sandbox.airwallex.com"
PRODUCTION_HOSTS = ("api.airwallex.com",)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    # Airwallex
    airwallex_mode: str = "mock"  # "mock" | "sandbox"
    airwallex_client_id: str = ""
    airwallex_api_key: str = ""
    airwallex_base_url: str = SANDBOX_BASE_URL
    airwallex_timeout_s: float = 20.0
    lookback_days: int = 30

    # Ledgerguard state
    invoice_book_path: str = ""
    ledger_path: str = ""
    approval_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    approval_ttl_s: int = 15 * 60
    webhook_secret: str = ""
    payer_entity_name: str = "Ledgerguard Demo Co"

    # LLM (any OpenAI-compatible /chat/completions with tool calling)
    llm_base_url: str = GROQ_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    llm_mock: bool = False
    llm_timeout_s: float = 45.0
    agent_max_steps: int = 10

    @property
    def use_mock(self) -> bool:
        return self.airwallex_mode != "sandbox"

    @property
    def use_llm_mock(self) -> bool:
        return self.llm_mock or not self.llm_api_key

    def validate(self) -> None:
        if self.airwallex_mode not in {"mock", "sandbox"}:
            raise ConfigError("AIRWALLEX_MODE must be 'mock' or 'sandbox'")
        if urlparse(self.airwallex_base_url).hostname in PRODUCTION_HOSTS:
            raise ConfigError("Ledgerguard is sandbox-only: refusing the production API host")
        if not self.use_mock and not (self.airwallex_client_id and self.airwallex_api_key):
            raise ConfigError(
                "AIRWALLEX_MODE=sandbox needs AIRWALLEX_CLIENT_ID and AIRWALLEX_API_KEY"
            )


def load_settings() -> Settings:
    kwargs: dict[str, object] = {}
    secret = os.getenv("APPROVAL_SECRET", "")
    if secret:
        kwargs["approval_secret"] = secret
    s = Settings(
        airwallex_mode=os.getenv("AIRWALLEX_MODE", "mock").strip().lower() or "mock",
        airwallex_client_id=os.getenv("AIRWALLEX_CLIENT_ID", ""),
        airwallex_api_key=os.getenv("AIRWALLEX_API_KEY", ""),
        airwallex_base_url=os.getenv("AIRWALLEX_BASE_URL", SANDBOX_BASE_URL).rstrip("/"),
        airwallex_timeout_s=float(os.getenv("AIRWALLEX_TIMEOUT_S", "20")),
        lookback_days=int(os.getenv("LOOKBACK_DAYS", "30")),
        invoice_book_path=os.getenv("INVOICE_BOOK_PATH", ""),
        ledger_path=os.getenv("LEDGER_PATH", ""),
        approval_ttl_s=int(os.getenv("APPROVAL_TTL_S", str(15 * 60))),
        webhook_secret=os.getenv("AIRWALLEX_WEBHOOK_SECRET", ""),
        payer_entity_name=os.getenv("PAYER_ENTITY_NAME", "Ledgerguard Demo Co"),
        llm_base_url=os.getenv("LLM_BASE_URL", GROQ_BASE_URL).rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        llm_mock=_flag("LLM_MOCK"),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "45")),
        agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", "10")),
        **kwargs,  # type: ignore[arg-type]
    )
    s.validate()
    return s
