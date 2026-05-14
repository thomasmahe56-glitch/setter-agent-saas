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
    business_name: str
    coach_name: str
    agent_name: str
    url_page: str
    url_call: str
    contact_email: str
    niche_context: str


def load_config() -> Config:
    return Config(
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_key=os.environ.get("SUPABASE_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        dashboard_secret=os.environ.get("DASHBOARD_SECRET", ""),
        manychat_token=os.environ.get("MANYCHAT_TOKEN", ""),
        business_name=os.environ.get("BUSINESS_NAME", ""),
        coach_name=os.environ.get("COACH_NAME", ""),
        agent_name=os.environ.get("AGENT_NAME", "Agent"),
        url_page=os.environ.get("URL_PAGE", ""),
        url_call=os.environ.get("URL_CALL", ""),
        contact_email=os.environ.get("CONTACT_EMAIL", ""),
        niche_context=os.environ.get("NICHE_CONTEXT", ""),
    )
