#!/bin/bash
# ============================================================
# [已废弃] 个人知识库 ECS 一键部署脚本
#
# ⚠️ 此脚本已废弃，请使用 GitHub Actions CD 流程部署。
# 当前部署方式：git push → GitHub Actions (.github/workflows/cd.yml) → 自动构建 + 部署到 ECS
# 详见：https://github.com/sammmad001/kb-Aidriven/blob/main/.github/workflows/cd.yml
#
# 本脚本仅供历史参考，不再维护。如需手动部署，请参考以下步骤：
#   1. 确保 config.py 中 llm_provider 默认值为 deepseek（V2.0 迁移后）
#   2. 所有的 LLM 调用已迁移到 DeepSeek API（不再依赖 Ollama / DashScope for LLM）
#   3. DashScope 仅保留用于 ASR (Paraformer) 和 OCR (qwen-vl-max)
#   4. 新增环境变量：DEEPSEEK_API_KEY, JWT_SECRET, MIROMIND_API_KEY 等
#
# 适用系统: Alibaba Cloud Linux / CentOS / Ubuntu
# 部署路径: /opt/knowledge-base
#
# 使用前请设置以下环境变量:
#   export NEO4J_PASSWORD=<your-neo4j-password>
#   export DEEPSEEK_API_KEY=<your-deepseek-key>  (V2.0: 替代 DASHSCOPE_API_KEY)
#   export DASHSCOPE_API_KEY=<your-dashscope-key>  (仅 ASR/OCR)
#   export FEISHU_APP_ID=<your-feishu-app-id>
#   export FEISHU_APP_SECRET=<your-feishu-app-secret>
#   export FEISHU_VERIFICATION_TOKEN=<your-verification-token>
#   export FEISHU_ENCRYPT_KEY=<your-encrypt-key>
#   export JWT_SECRET=<your-jwt-secret>  (V2.0: 多用户认证)
#   export MIROMIND_API_KEY=<your-miromind-key>  (V2.0: 深度研究)
#   export KNOWLEDGE_API_TOKEN=<your-api-token>
# ============================================================

set -e

echo "===== [1/6] 安装基础依赖 ====="
if command -v apt-get &>/dev/null; then
    apt-get update && apt-get install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx wget curl
elif command -v yum &>/dev/null; then
    yum install -y python3 python3-pip nginx wget curl
    pip3 install certbot python3-certbot-nginx 2>/dev/null || true
fi

echo "===== [2/6] 安装 JDK 21 ====="
if ! java -version 2>&1 | grep -q "21"; then
    ARCH=$(uname -m)
    if [ "$ARCH" = "aarch64" ]; then
        JDK_URL="https://cdn.azul.com/zulu/bin/zulu21.40.17-ca-jdk21.0.6-linux_aarch64.tar.gz"
    else
        JDK_URL="https://cdn.azul.com/zulu/bin/zulu21.40.17-ca-jdk21.0.6-linux_x64.tar.gz"
    fi
    mkdir -p /opt/tools
    wget -q "$JDK_URL" -O /tmp/jdk21.tar.gz
    tar -xzf /tmp/jdk21.tar.gz -C /opt/tools/
    rm /tmp/jdk21.tar.gz
    JDK_HOME=$(ls -d /opt/tools/zulu21* | head -1)
    export JAVA_HOME="$JDK_HOME"
    export PATH="$JAVA_HOME/bin:$PATH"
    echo "export JAVA_HOME=$JDK_HOME" >> /etc/profile.d/jdk21.sh
    echo "export PATH=\$JAVA_HOME/bin:\$PATH" >> /etc/profile.d/jdk21.sh
    echo "JDK 21 安装完成: $JAVA_HOME"
else
    echo "JDK 21 已存在"
fi

echo "===== [3/6] 安装 Neo4j ====="
if ! command -v neo4j &>/dev/null; then
    NEO4J_VERSION="2026.05.0"
    NEO4J_URL="https://dist.neo4j.org/neo4j-community-${NEO4J_VERSION}-unix.tar.gz"
    wget -q --header="User-Agent: Mozilla/5.0" "$NEO4J_URL" -O /tmp/neo4j.tar.gz || \
    python3 -c "
import urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
urllib.request.urlretrieve('$NEO4J_URL', '/tmp/neo4j.tar.gz')
"
    tar -xzf /tmp/neo4j.tar.gz -C /opt/
    rm /tmp/neo4j.tar.gz
    NEO4J_HOME=$(ls -d /opt/neo4j-community-* | head -1)
    ln -sf "$NEO4J_HOME" /opt/neo4j
    echo "export NEO4J_HOME=$NEO4J_HOME" >> /etc/profile.d/neo4j.sh
    echo "export PATH=\$NEO4J_HOME/bin:\$PATH" >> /etc/profile.d/neo4j.sh
    export NEO4J_HOME="$NEO4J_HOME"
    export PATH="$NEO4J_HOME/bin:$PATH"

    # 设置密码（从环境变量读取）
    neo4j-admin dbms set-initial-password "${NEO4J_PASSWORD:?请设置环境变量 NEO4J_PASSWORD}" 2>/dev/null || true

    # 配置 Neo4j 监听所有接口
    sed -i 's/#server.default_listen_address=0.0.0.0/server.default_listen_address=0.0.0.0/' "$NEO4J_HOME/conf/neo4j.conf"

    # 启动 Neo4j
    neo4j start
    echo "Neo4j 已启动"
else
    echo "Neo4j 已安装"
fi

echo "===== [4/6] 部署应用代码 ====="
APP_DIR="/opt/knowledge-base"
mkdir -p "$APP_DIR"

# 创建专用运行用户
if ! id -u kbuser &>/dev/null; then
    useradd -r -s /sbin/nologin kbuser
    echo "用户 kbuser 已创建"
fi

# 创建应用文件
cat > "$APP_DIR/app/__init__.py" << 'PYEOF'
PYEOF

mkdir -p "$APP_DIR/app/api" "$APP_DIR/app/feishu" "$APP_DIR/app/ingest" "$APP_DIR/app/lint" "$APP_DIR/app/query" "$APP_DIR/tests"

# 从本地 Mac 传输代码（需要先在 Mac 上执行 scp）
# scp -r /Users/sam/Desktop/个人知识库/kb/* <your-server>:/opt/knowledge-base/

echo "请确保代码已传输到 $APP_DIR"
echo "在 Mac 上执行: scp -r /Users/sam/Desktop/个人知识库/kb/* <your-server>:/opt/knowledge-base/"

echo "===== [5/6] 安装 Python 依赖 ====="
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install fastapi 'uvicorn[standard]' neo4j httpx pydantic python-dotenv pytest pytest-asyncio respx anyio 'lark-oapi>=1.0.0'

echo "===== [6/6] 配置并启动服务 ====="

# 创建 .env 文件（从环境变量读取敏感配置）
cat > "$APP_DIR/.env" << ENVEOF
# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=${NEO4J_PASSWORD:?未设置 NEO4J_PASSWORD}

# LLM Provider
LLM_PROVIDER=dashscope

# DashScope (Alibaba Cloud / 百炼)
DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY:?未设置 DASHSCOPE_API_KEY}
DASHSCOPE_MODEL=qwen3.5-plus

# DashScope 分环节模型分配
DASHSCOPE_MODEL_ANALYZE=qwen-turbo
DASHSCOPE_MODEL_COMPILE=qwen3.5-plus
DASHSCOPE_MODEL_REASONING=qwen-flash
DASHSCOPE_MODEL_QUERY=qwen3.5-plus

# Feishu App
FEISHU_APP_ID=${FEISHU_APP_ID:?未设置 FEISHU_APP_ID}
FEISHU_APP_SECRET=${FEISHU_APP_SECRET:?未设置 FEISHU_APP_SECRET}
FEISHU_VERIFICATION_TOKEN=${FEISHU_VERIFICATION_TOKEN:?未设置 FEISHU_VERIFICATION_TOKEN}
FEISHU_ENCRYPT_KEY=${FEISHU_ENCRYPT_KEY:?未设置 FEISHU_ENCRYPT_KEY}

# Knowledge API
KNOWLEDGE_API_TOKEN=${KNOWLEDGE_API_TOKEN:?KNOWLEDGE_API_TOKEN must be set}

# Environment
ENVIRONMENT=production

# CORS (restrict to your frontend domain)
CORS_ORIGINS=${CORS_ORIGINS:-*}
ENVEOF

# 设置目录权限
chown -R kbuser:kbuser "$APP_DIR"

# 创建 systemd 服务
cat > /etc/systemd/system/knowledge-base.service << 'SVCEOF'
[Unit]
Description=Knowledge Base API
After=network.target neo4j.service

[Service]
Type=simple
User=kbuser
Group=kbuser
WorkingDirectory=/opt/knowledge-base
ExecStart=/opt/knowledge-base/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment=JAVA_HOME=/opt/tools/jdk

[Install]
WantedBy=multi-user.target
SVCEOF

# 修正 JAVA_HOME 路径
ACTUAL_JDK=$(ls -d /opt/tools/zulu21* 2>/dev/null | head -1)
if [ -n "$ACTUAL_JDK" ]; then
    sed -i "s|Environment=JAVA_HOME=.*|Environment=JAVA_HOME=$ACTUAL_JDK|" /etc/systemd/system/knowledge-base.service
fi

systemctl daemon-reload
systemctl enable knowledge-base
systemctl start knowledge-base

echo ""
echo "============================================"
echo "  部署完成！"
echo "  服务地址: http://<YOUR_SERVER_IP>:8080"
echo "  健康检查: curl http://localhost:8080/health"
echo "  查看日志: journalctl -u knowledge-base -f"
echo "============================================"
echo ""
echo "下一步: 配置 Nginx + HTTPS (需要域名)"
echo "  如果有域名，运行: certbot --nginx -d your-domain.com"
