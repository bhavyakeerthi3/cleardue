from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    mode: Literal["fixture", "live"] = Field("fixture", alias="CLEARDUE_MODE")
    db_path: Path = Field(ROOT / "data" / "cleardue.db", alias="CLEARDUE_DB_PATH")
    session_secret: str = Field("development-only-change-me", alias="CLEARDUE_SESSION_SECRET")
    operator_password: str = Field("cleardue-demo", alias="CLEARDUE_OPERATOR_PASSWORD")
    operator_name: str = Field("demo-operator", alias="CLEARDUE_OPERATOR_NAME")

    reasoning_provider: Literal["gemini"] = Field("gemini", alias="REASONING_PROVIDER")
    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-3.6-flash", alias="GEMINI_MODEL")
    watch_interval_seconds: int = Field(60, ge=10, alias="CLEARDUE_WATCH_SECONDS")
    watch_enabled: bool = Field(False, alias="CLEARDUE_WATCH_ENABLED")
    approval_ttl_seconds: int = Field(300, ge=1, alias="CLEARDUE_APPROVAL_TTL_SECONDS")

    google_client_secret_file: Path = Field(
        ROOT / "data" / "google-client-secret.json", alias="GOOGLE_CLIENT_SECRET_FILE"
    )
    google_token_file: Path = Field(
        ROOT / "data" / "google-token.json", alias="GOOGLE_TOKEN_FILE"
    )
    gmail_mailbox_ref: str = Field("me", alias="GMAIL_MAILBOX_REF")

    jira_base_url: str | None = Field(None, alias="JIRA_BASE_URL")
    jira_email: str | None = Field(None, alias="JIRA_EMAIL")
    jira_api_token: str | None = Field(None, alias="JIRA_API_TOKEN")
    jira_project_key: str = Field("", alias="JIRA_PROJECT_KEY")
    jira_issue_type: str = Field("Task", alias="JIRA_ISSUE_TYPE")

    razorpay_key_id: str | None = Field(None, alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str | None = Field(None, alias="RAZORPAY_KEY_SECRET")
    razorpay_account_ref: str = Field("test-account", alias="RAZORPAY_ACCOUNT_REF")

    demo_case_id: str = Field("case-cleardue-001", alias="DEMO_CASE_ID")
    demo_invoice_id: str | None = Field(None, alias="DEMO_INVOICE_ID")
    demo_approver_email: str | None = Field(None, alias="DEMO_APPROVER_EMAIL")
    demo_project_ref: str | None = Field(None, alias="DEMO_PROJECT_REF")
    demo_jira_issue_ids: str = Field("", alias="DEMO_JIRA_ISSUE_IDS")

    @property
    def demo_jira_issue_id_list(self) -> list[str]:
        return [item.strip() for item in self.demo_jira_issue_ids.split(",") if item.strip()]

    @model_validator(mode="after")
    def resolve_paths(self) -> "Settings":
        if not self.db_path.is_absolute():
            self.db_path = ROOT / self.db_path
        if not self.google_client_secret_file.is_absolute():
            self.google_client_secret_file = ROOT / self.google_client_secret_file
        if not self.google_token_file.is_absolute():
            self.google_token_file = ROOT / self.google_token_file
        return self

    def live_missing(self) -> list[str]:
        missing: list[str] = []
        checks = {
            "GEMINI_API_KEY": bool(self.gemini_api_key),
            "GOOGLE_CLIENT_SECRET_FILE": self.google_client_secret_file.exists(),
            "JIRA_BASE_URL": bool(self.jira_base_url),
            "JIRA_EMAIL": bool(self.jira_email),
            "JIRA_API_TOKEN": bool(self.jira_api_token),
            "JIRA_PROJECT_KEY": bool(self.jira_project_key),
            "RAZORPAY_KEY_ID": bool(self.razorpay_key_id),
            "RAZORPAY_KEY_SECRET": bool(self.razorpay_key_secret),
            "DEMO_INVOICE_ID": bool(self.demo_invoice_id),
            "DEMO_APPROVER_EMAIL": bool(self.demo_approver_email),
            "DEMO_PROJECT_REF": bool(self.demo_project_ref),
            "DEMO_JIRA_ISSUE_IDS": bool(self.demo_jira_issue_id_list),
        }
        for name, present in checks.items():
            if not present:
                missing.append(name)
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
