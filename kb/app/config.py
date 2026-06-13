"""Configuration management using pydantic-settings."""

import logging
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Global application settings loaded from environment variables / .env file."""

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""  # SECURITY: No default — must be set via env

    # --- LLM ---
    llm_provider: Literal["ollama", "dashscope"] = "ollama"

    # Ollama (local)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    # DashScope (Alibaba Cloud / 百炼)
    dashscope_api_key: str = ""
    dashscope_model: str = "qwen3.5-plus"  # 默认模型（兼容旧配置）

    # DashScope 分环节模型分配（V1.1优化：隐式推理从 flash 升级到 plus）
    dashscope_model_analyze: str = "qwen-turbo"     # Step 2 分析分类（轻量任务）
    dashscope_model_compile: str = "qwen3.5-plus"   # Step 3b 页面编写（质量关键）
    dashscope_model_reasoning: str = "qwen3.5-plus"  # Step 3c 隐式推理（P0升级：flash→plus）
    dashscope_model_query: str = "qwen3.5-plus"     # Query 回答生成（用户面向）

    # --- Feishu ---
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_use_ws: bool = False  # True=WebSocket长连接, False=Webhook HTTP推送

    # --- Knowledge API ---
    knowledge_api_token: str = ""  # SECURITY: No default — must be set via env

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
    cors_origins: str = "*"  # Comma-separated origins; "*" allows all

    # --- Environment ---
    environment: Literal["dev", "production"] = "dev"

    # --- Social Media Fetcher ---
    social_xhs_cookie: str = ""          # 小红书Cookie字符串（a1=xxx; web_session=xxx; ...）
    social_weibo_cookie: str = ""        # 微博Cookie字符串（SUB=xxx; ...）
    social_fetch_timeout: int = 60       # 单次抓取超时秒数
    social_ocr_paddle_enabled: bool = True  # 是否启用PaddleOCR（免费本地OCR）
    social_ocr_dashscope_key: str = ""   # OCR兜底用的DashScope API Key（为空时仅用PaddleOCR）

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_production_config(self) -> tuple[list[str], list[str]]:
        """Validate that all required settings are present for production mode.

        Returns (errors, warnings). Errors block startup; warnings are logged.
        """
        errors: list[str] = []
        warnings: list[str] = []
        if not self.neo4j_password:
            errors.append("NEO4J_PASSWORD is required in production")
        if not self.knowledge_api_token:
            errors.append("KNOWLEDGE_API_TOKEN is required in production")
        if self.llm_provider == "dashscope" and not self.dashscope_api_key:
            errors.append("DASHSCOPE_API_KEY is required when llm_provider=dashscope")
        if self.cors_origins == "*":
            warnings.append("CORS_ORIGINS is set to '*' — restrict to specific origins in production")
        return errors, warnings


@lru_cache
def get_settings() -> Settings:
    """Return a cached global Settings singleton."""
    return Settings()
