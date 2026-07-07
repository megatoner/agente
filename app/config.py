from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "Odoo Agents Server"
    debug: bool = False
    api_key: str = "cambiar_esta_key_por_una_segura_12345"
    database_url: str = "postgresql+psycopg2://odoo_agents:agents_pass_2024@localhost:5432/odoo_agents"
    
    # Odoo
    odoo_url: str = "http://10.200.200.2:8069"
    odoo_db: str = "odoo"
    odoo_username: str = "admin"
    odoo_api_key: str = ""
    
    # LLM Providers
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    
    default_model: str = "openai/gpt-4o-mini"
    
    class Config:
        env_file = "/opt/odoo-agents/.env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
