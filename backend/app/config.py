"""Typed settings loaded from backend/.env. Import `settings` everywhere; never read os.environ directly."""
from decimal import Decimal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"
    log_level: str = "INFO"

    # PayPal
    paypal_mode: str = "sandbox"
    paypal_api_base: str = "https://api-m.sandbox.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_webhook_id: str = ""
    paypal_currency: str = "USD"

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    database_url: SecretStr = SecretStr("")

    # AI: provider is groq | ollama. AI_MODEL empty means the provider's default.
    ai_provider: str = "groq"
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    ollama_base_url: str = "http://localhost:11434/v1"
    ai_model: str = ""
    ai_temperature: float = 0.2
    stt_model: str = "whisper-large-v3-turbo"  # Groq speech-to-text for voice questions
    ai_max_tokens: int = 2048
    ai_timeout_seconds: int = 60

    @property
    def ai_model_name(self) -> str:
        defaults = {"groq": "openai/gpt-oss-120b", "ollama": "llama3.1:8b"}
        return self.ai_model.strip() or defaults.get(self.ai_provider, "")

    def ai_key_name(self) -> str | None:
        return {"groq": "GROQ_API_KEY"}.get(self.ai_provider)

    def ai_key(self) -> str:
        secret = {"groq": self.groq_api_key}.get(self.ai_provider)
        return secret.get_secret_value().strip() if secret else ""

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider == "ollama" or bool(self.ai_key())

    # APIMatic
    apimatic_api_key: SecretStr = SecretStr("")

    # Auth
    jwt_secret: SecretStr = SecretStr("")
    jwt_expire_minutes: int = 480
    demo_admin_email: str = "admin@paypap.demo"
    demo_admin_password: SecretStr = SecretStr("")
    demo_approver_email: str = "approver@paypap.demo"
    demo_approver_password: SecretStr = SecretStr("")

    # Approval gate
    approval_signing_secret: SecretStr = SecretStr("")
    approval_ttl_minutes: int = 60

    # Rules caps
    max_amount_per_recipient: Decimal = Decimal("1000.00")
    max_batch_total: Decimal = Decimal("25000.00")
    max_batch_items: int = 500
    suspicious_amount_zscore: float = 3.0
    min_amount: Decimal = Decimal("0.01")

    # Status tracking
    status_poll_interval_seconds: int = 30
    status_poll_max_minutes: int = 60
    cron_secret: SecretStr = SecretStr("")  # Vercel sends it as "Authorization: Bearer ..." on cron calls

    def missing_required(self) -> list[str]:
        """Names of keys that must be filled before the app can talk to external services."""
        required = {
            "PAYPAL_CLIENT_ID": self.paypal_client_id,
            "PAYPAL_CLIENT_SECRET": self.paypal_client_secret.get_secret_value(),
            "PAYPAL_WEBHOOK_ID": self.paypal_webhook_id,
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key.get_secret_value(),
            "JWT_SECRET": self.jwt_secret.get_secret_value(),
            "APPROVAL_SIGNING_SECRET": self.approval_signing_secret.get_secret_value(),
            "DEMO_ADMIN_PASSWORD": self.demo_admin_password.get_secret_value(),
            "DEMO_APPROVER_PASSWORD": self.demo_approver_password.get_secret_value(),
        }
        if self.ai_key_name():
            required[self.ai_key_name()] = self.ai_key()
        return [k for k, v in required.items() if not v]


settings = Settings()
