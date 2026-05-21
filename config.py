import os
from dataclasses import dataclass


@dataclass
class Config:
    supabase_url: str
    supabase_key: str
    anthropic_api_key: str
    webhook_secret: str
    dashboard_secret: str
    manychat_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    meta_app_secret: str
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


def load_config() -> Config:
    return Config(
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_key=os.environ.get("SUPABASE_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        dashboard_secret=os.environ.get("DASHBOARD_SECRET", ""),
        manychat_token=os.environ.get("MANYCHAT_TOKEN", ""),
        whatsapp_access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        meta_app_secret=os.environ.get("META_APP_SECRET", ""),
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
    )
