"""
集中配置管理 - 从 .env 文件加载配置

变更说明（个人知识库集成）：
- 新增 KB_API_BASE、KB_API_TOKEN、KB_AUTO_INGEST 三个配置项
"""
import os
from pathlib import Path

# 加载 .env 文件
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# MiroMind API
MIROMIND_API_BASE: str = _get("MIROMIND_API_BASE", "https://api.miromind.ai/v1")
MIROMIND_API_KEY: str = _get("MIROMIND_API_KEY", "")
DEFAULT_MODEL: str = _get("DEFAULT_MODEL", "mirothinker-1-7-deepresearch")

# 服务配置
PORT: int = int(_get("PORT", "8900"))
REQUEST_TIMEOUT: int = int(_get("REQUEST_TIMEOUT", "300"))
BASE_PATH: str = _get("BASE_PATH", "/miro")

# JWT 认证
JWT_SECRET: str = _get("JWT_SECRET", "")
JWT_ALGORITHM: str = _get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS: int = int(_get("JWT_EXPIRE_HOURS", "168"))

# Cookie 安全
COOKIE_SECURE: bool = _get("COOKIE_SECURE", "false").lower() == "true"

# 搜索引导指令（引导模型优先引用国际/英文来源）
SEARCH_INSTRUCTION: str = _get("SEARCH_INSTRUCTION", "[Research Preference] When searching for information and providing references, prioritize international and English-language sources (e.g., Wikipedia EN, arXiv, Reuters, Bloomberg, Nature, IEEE, official government/organization websites). Avoid citing Chinese domestic websites unless the topic specifically requires Chinese sources. All reference links should point to the original English/international version whenever possible.")

# SQLite 数据库
DATABASE_PATH: str = _get("DATABASE_PATH", "data/miromind.db")

# ── 个人知识库集成配置 ──
# KB_API_BASE: 知识库 API 地址（默认本地 8080 端口）
KB_API_BASE: str = _get("KB_API_BASE", "http://localhost:8080")
# KB_API_TOKEN: 知识库 API 认证令牌（需与 KB 侧 KNOWLEDGE_API_TOKEN 一致）
KB_API_TOKEN: str = _get("KB_API_TOKEN", "")
# KB_AUTO_INGEST: 是否自动导入聊天内容到知识库（"true" / "false"）
KB_AUTO_INGEST: str = _get("KB_AUTO_INGEST", "true")

# 可用模型列表
MODELS = [
    {
        "id": "mirothinker-1-7-deepresearch",
        "name": "MiroThinker Deep Research",
        "badge": "旗舰",
        "description": "深度多步推理+多轮搜索，适合复杂分析、金融研究、学术论文",
        "input_price": "$4.00/Mtok",
        "context_window": "256K",
        "max_tool_calls": 300,
        "use_cases": "复杂分析、金融研究、学术论文",
    },
    {
        "id": "mirothinker-1-7-deepresearch-mini",
        "name": "MiroThinker Mini",
        "badge": "快速",
        "description": "快速推理，轻量研究，适合日常问答和快速检索",
        "input_price": "$1.25/Mtok",
        "context_window": "256K",
        "max_tool_calls": 100,
        "use_cases": "日常问答、快速检索",
    },
]
