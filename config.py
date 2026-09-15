import os
from dataclasses import dataclass


DEFAULT_OPENAI_SETTER_MODEL = "gpt-5.6-luna"
DEFAULT_ANTHROPIC_SETTER_MODEL = "claude-sonnet-4-6"


@dataclass
class Config:
    supabase_url: str
    supabase_key: str
    anthropic_api_key: str
    openai_api_key: str
    setter_primary_provider: str
    setter_primary_model: str
    setter_premium_provider: str
    setter_premium_model: str
    setter_openai_enabled: bool
    setter_context_compression_enabled: bool
    setter_persistent_idempotency_enabled: bool
    setter_premium_escalation_enabled: bool
    webhook_secret: str
    dashboard_secret: str
    manychat_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    meta_app_secret: str
    meta_app_id: str
    meta_webhook_verify_token: str
    meta_instagram_redirect_uri: str
    meta_instagram_post_connect_url: str
    meta_graph_api_version: str
    messaging_token_encryption_key: str
    meta_instagram_enabled: bool
    meta_instagram_oauth_enabled: bool
    meta_instagram_webhook_enabled: bool
    meta_instagram_send_enabled: bool
    meta_instagram_history_sync_enabled: bool
    meta_instagram_reply_window_hours: int
    graph_api_version: str
    business_name: str
    coach_name: str
    agent_name: str
    url_page: str
    url_call: str
    contact_email: str
    niche_context: str
    supabase_jwt_secret: str
    owner_user_id: str
    allowed_user_ids: str
    cors_allowed_origins: str
    commercial_conversion_enabled: bool
    commercial_lead_server_token: str
    environment: str


def load_config() -> Config:
    def env_bool(name: str, default: bool) -> bool:
        return os.environ.get(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}

    return Config(
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_key=os.environ.get("SUPABASE_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        setter_primary_provider=os.environ.get("SETTER_PRIMARY_PROVIDER", "anthropic").strip().lower(),
        setter_primary_model=os.environ.get("SETTER_PRIMARY_MODEL", "").strip(),
        setter_premium_provider=os.environ.get("SETTER_PREMIUM_PROVIDER", "anthropic").strip().lower(),
        setter_premium_model=os.environ.get("SETTER_PREMIUM_MODEL", DEFAULT_ANTHROPIC_SETTER_MODEL).strip(),
        setter_openai_enabled=env_bool("SETTER_OPENAI_ENABLED", False),
        setter_context_compression_enabled=env_bool("SETTER_CONTEXT_COMPRESSION_ENABLED", False),
        setter_persistent_idempotency_enabled=env_bool("SETTER_PERSISTENT_IDEMPOTENCY_ENABLED", False),
        setter_premium_escalation_enabled=env_bool("SETTER_PREMIUM_ESCALATION_ENABLED", False),
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        dashboard_secret=os.environ.get("DASHBOARD_SECRET", ""),
        manychat_token=os.environ.get("MANYCHAT_TOKEN", ""),
        whatsapp_access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        meta_app_secret=os.environ.get("META_APP_SECRET", ""),
        meta_app_id=os.environ.get("META_APP_ID", ""),
        meta_webhook_verify_token=os.environ.get("META_WEBHOOK_VERIFY_TOKEN", ""),
        meta_instagram_redirect_uri=os.environ.get("META_INSTAGRAM_REDIRECT_URI", ""),
        meta_instagram_post_connect_url=os.environ.get("META_INSTAGRAM_POST_CONNECT_URL", "http://localhost:3000/settings/integrations"),
        meta_graph_api_version=os.environ.get("META_GRAPH_API_VERSION", "v26.0").strip(),
        messaging_token_encryption_key=os.environ.get("MESSAGING_TOKEN_ENCRYPTION_KEY", ""),
        meta_instagram_enabled=env_bool("META_INSTAGRAM_ENABLED", False),
        meta_instagram_oauth_enabled=env_bool("META_INSTAGRAM_OAUTH_ENABLED", False),
        meta_instagram_webhook_enabled=env_bool("META_INSTAGRAM_WEBHOOK_ENABLED", False),
        meta_instagram_send_enabled=env_bool("META_INSTAGRAM_SEND_ENABLED", False),
        meta_instagram_history_sync_enabled=env_bool("META_INSTAGRAM_HISTORY_SYNC_ENABLED", False),
        meta_instagram_reply_window_hours=min(24, max(1, int(os.environ.get("META_INSTAGRAM_REPLY_WINDOW_HOURS", "24")))),
        graph_api_version=os.environ.get("GRAPH_API_VERSION", "v23.0"),
        business_name=os.environ.get("BUSINESS_NAME", ""),
        coach_name=os.environ.get("COACH_NAME", ""),
        agent_name=os.environ.get("AGENT_NAME", "Agent"),
        url_page=os.environ.get("URL_PAGE", ""),
        url_call=os.environ.get("URL_CALL", ""),
        contact_email=os.environ.get("CONTACT_EMAIL", ""),
        niche_context=os.environ.get("NICHE_CONTEXT", ""),
        supabase_jwt_secret=os.environ.get("SUPABASE_JWT_SECRET", ""),
        owner_user_id=os.environ.get("OWNER_USER_ID", ""),
        allowed_user_ids=os.environ.get("ALLOWED_USER_IDS", ""),
        cors_allowed_origins=os.environ.get("CORS_ALLOWED_ORIGINS", ""),
        commercial_conversion_enabled=env_bool("COMMERCIAL_CONVERSION_ENABLED", False),
        commercial_lead_server_token=os.environ.get("COMMERCIAL_LEAD_SERVER_TOKEN", ""),
        environment=os.environ.get("ENVIRONMENT", "development"),
    )
