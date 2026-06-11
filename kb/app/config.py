"""Configuration management using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings loaded from environment variables / .env file."""

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "your_neo4j_password"

    # --- LLM ---
    llm_provider: Literal["ollama", "dashscope"] = "ollama"

    # Ollama (local)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    # DashScope (Alibaba Cloud / 百炼)
    dashscope_api_key: str = ""
    dashscope_model: str = "qwen3.5-plus"  # 默认模型（兼容旧配置）

    # DashScope 分环节模型分配（推荐方案：性能/成本最优平衡）
    dashscope_model_analyze: str = "qwen-turbo"     # Step 2 分析分类
    dashscope_model_compile: str = "qwen3.5-plus"   # Step 3b 页面编写
    dashscope_model_reasoning: str = "qwen-flash"   # Step 3d 隐式推理
    dashscope_model_query: str = "qwen3.5-plus"     # Query 回答生成

    # --- Feishu ---
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_use_ws: bool = False  # True=WebSocket长连接, False=Webhook HTTP推送

    # --- Knowledge API ---
    knowledge_api_token: str = "dev-token"

    # --- Paths ---
    raw_dir: str = "raw/sources"
    wiki_dir: str = "wiki"
    raw_ingest_dir: str = "raw/ingest"  # channel adapter raw JSON for retry

    # --- Ingest Automation ---
    ingest_auto_retry: bool = True
    ingest_retry_interval_minutes: int = 5

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached global Settings singleton."""
    return Settings()
