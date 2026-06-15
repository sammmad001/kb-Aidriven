#!/bin/bash
# ============================================================
# 个人知识库 ECS 增量部署/重新部署脚本
# 部署路径: /opt/knowledge-base
#
# 使用前请设置以下环境变量:
#   export NEO4J_PASSWORD=<your-neo4j-password>
#   export DASHSCOPE_API_KEY=<your-dashscope-key>
#   export FEISHU_APP_ID=<your-feishu-app-id>
#   export FEISHU_APP_SECRET=<your-feishu-app-secret>
#   export FEISHU_VERIFICATION_TOKEN=<your-verification-token>
#   export FEISHU_ENCRYPT_KEY=<your-encrypt-key>
#   export KNOWLEDGE_API_TOKEN=<your-api-token>
# ============================================================
set -e
echo "===== [1/5] 安装 JDK 21 ====="
if /opt/tools/zulu21*/bin/java -version 2>&1 | grep -q "21"; then
    echo "JDK 21 已存在"
else
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
    echo "JDK 21 下载完成"
fi
JDK_HOME=$(ls -d /opt/tools/zulu21* | head -1)
echo "export JAVA_HOME=$JDK_HOME" > /etc/profile.d/jdk21.sh
echo 'export PATH=$JAVA_HOME/bin:$PATH' >> /etc/profile.d/jdk21.sh
export JAVA_HOME="$JDK_HOME"
export PATH="$JAVA_HOME/bin:$PATH"
java -version 2>&1
echo "JDK_HOME=$JDK_HOME"

echo ""
echo "===== [2/5] 安装 Neo4j ====="
export JAVA_HOME="$JDK_HOME"
export PATH="$JAVA_HOME/bin:$PATH"
if [ -d /opt/neo4j ]; then
    echo "Neo4j 目录已存在"
else
    NEO4J_VERSION="2026.05.0"
    NEO4J_URL="https://dist.neo4j.org/neo4j-community-${NEO4J_VERSION}-unix.tar.gz"
    echo "下载 Neo4j $NEO4J_VERSION ..."
    wget -q --header="User-Agent: Mozilla/5.0" "$NEO4J_URL" -O /tmp/neo4j.tar.gz 2>/dev/null || \
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
    echo "Neo4j 安装完成: $NEO4J_HOME"
fi
NEO4J_HOME=$(ls -d /opt/neo4j-community-* 2>/dev/null | head -1 || echo "/opt/neo4j")
echo "export NEO4J_HOME=$NEO4J_HOME" > /etc/profile.d/neo4j.sh
echo 'export PATH=$NEO4J_HOME/bin:$PATH' >> /etc/profile.d/neo4j.sh
export NEO4J_HOME="$NEO4J_HOME"
export PATH="$NEO4J_HOME/bin:$PATH"

# 设置密码（从环境变量读取）
neo4j-admin dbms set-initial-password "${NEO4J_PASSWORD:?请设置环境变量 NEO4J_PASSWORD}" 2>/dev/null || true

# 配置监听所有接口
sed -i 's/#server.default_listen_address=0.0.0.0/server.default_listen_address=0.0.0.0/' "$NEO4J_HOME/conf/neo4j.conf" 2>/dev/null || true

# 启动 Neo4j
neo4j start || echo "Neo4j 可能已在运行"
sleep 3
echo "Neo4j 状态:"
neo4j status 2>&1 || true

echo ""
echo "===== [3/5] 更新代码和依赖 ====="
cd /opt/knowledge-base
# 清理 macOS 资源分叉文件
find . -name "._*" -delete 2>/dev/null || true

# 安装 Python 依赖
source venv/bin/activate
pip install --upgrade pip -q
pip install fastapi 'uvicorn[standard]' neo4j httpx pydantic python-dotenv pytest pytest-asyncio respx anyio 'lark-oapi>=1.0.0' -q
echo "Python 依赖安装完成"
pip list 2>/dev/null | grep -iE "fastapi|uvicorn|neo4j|lark"

echo ""
echo "===== [4/5] 配置 .env ====="
cat > /opt/knowledge-base/.env << ENVEOF
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=${NEO4J_PASSWORD:?未设置 NEO4J_PASSWORD}
LLM_PROVIDER=dashscope
DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY:?未设置 DASHSCOPE_API_KEY}
DASHSCOPE_MODEL=qwen3.5-plus
DASHSCOPE_MODEL_ANALYZE=qwen-turbo
DASHSCOPE_MODEL_COMPILE=qwen3.5-plus
DASHSCOPE_MODEL_REASONING=qwen-flash
DASHSCOPE_MODEL_QUERY=qwen3.5-plus
FEISHU_APP_ID=${FEISHU_APP_ID:?未设置 FEISHU_APP_ID}
FEISHU_APP_SECRET=${FEISHU_APP_SECRET:?未设置 FEISHU_APP_SECRET}
FEISHU_VERIFICATION_TOKEN=${FEISHU_VERIFICATION_TOKEN:?未设置 FEISHU_VERIFICATION_TOKEN}
FEISHU_ENCRYPT_KEY=${FEISHU_ENCRYPT_KEY:?未设置 FEISHU_ENCRYPT_KEY}
KNOWLEDGE_API_TOKEN=${KNOWLEDGE_API_TOKEN:-dev-token}
ENVEOF
echo ".env 配置完成"

echo ""
echo "===== [5/5] 配置 systemd 服务 ====="

# Ensure kbuser exists (consistent with deploy-ecs.sh)
if ! id kbuser &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -d /opt/knowledge-base kbuser 2>/dev/null || true
    echo "Created kbuser system user"
fi
chown -R kbuser:kbuser /opt/knowledge-base 2>/dev/null || true

ACTUAL_JDK=$(ls -d /opt/tools/zulu21* 2>/dev/null | head -1)
cat > /etc/systemd/system/knowledge-base.service << SVCEOF
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
Environment=JAVA_HOME=$ACTUAL_JDK
Environment=NEO4J_HOME=$NEO4J_HOME
Environment=PATH=/opt/knowledge-base/venv/bin:$ACTUAL_JDK/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable knowledge-base
systemctl restart knowledge-base
sleep 3
echo "服务状态:"
systemctl status knowledge-base --no-pager | head -15

echo ""
echo "============================================"
echo "  部署完成！"
echo "  健康检查: curl http://localhost:8080/health"
echo "  查看日志: journalctl -u knowledge-base -f"
echo "============================================"
