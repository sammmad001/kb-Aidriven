# 社交媒体抓取与 OCR 方案文档

> **版本**：v1.0
> **日期**：2025-01-25
> **状态**：已实现
> **关联模块**：`kb/app/services/social_fetcher.py`、`kb/app/ingest/ocr.py`

---

## 1. 设计背景

用户在飞书 Bot 中分享社交媒体链接（小红书、微博）时，系统需要自动：

1. **检测 URL**：从用户消息中识别社交媒体链接
2. **抓取内容**：绕过平台的反爬机制，获取帖子正文、图片、标签
3. **OCR 提取**：对图片中的文字进行 OCR 识别
4. **结构化入库**：将抓取到的文本和 OCR 文本合并后传入 Ingest 管道

### 设计挑战

| 挑战 | 解决方案 |
|------|---------|
| 小红书反爬严格（Cookie/设备指纹） | Playwright + playwright-stealth + Cookie 注入 |
| 小红书短链跳转循环 | httpx 预解析短链 + 浏览器导航计数器 |
| 微博页面 JS 渲染重 | JSON API 优先 + Playwright fallback |
| 图片 OCR 成本 | PaddleOCR 免费 + qwen-vl-max 付费 fallback |
| ECS 环境无 /dev/shm | Chromium 启动参数 `--disable-dev-shm-usage` |

---

## 2. 整体架构

```
用户飞书消息
    │ "https://xhslink.com/abc123"
    ▼
┌──────────────────────────────────────────────┐
│ social_fetcher.py                            │
│  └─ detect_social_url(text)                  │
│      → (SocialPlatform.XIAOHONGSHU, url)     │
└─────────────────┬────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────┐
│ SocialFetcher                                │
│  └─ fetch(url, platform) → SocialContent     │
│      ├─ _fetch_xiaohongshu()                 │
│      └─ _fetch_weibo()                       │
│          ├─ _fetch_weibo_api()  (JSON API)   │
│          └─ _fetch_weibo_playwright() (fallback) │
└─────────────────┬────────────────────────────┘
                  │
                  ▼ (SocialContent.images)
┌──────────────────────────────────────────────┐
│ ocr.py — ImageOCRExtractor                   │
│  └─ extract_batch(images) → OCRResult[]      │
│      ├─ PaddleOCR (免费，置信度阈值 0.6)       │
│      └─ qwen-vl-max (付费 fallback)           │
└─────────────────┬────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────┐
│ 合并文本 → Preprocessor → Ingest 管道        │
│  正文 + 标签 + OCR文本 → Neo4j               │
└──────────────────────────────────────────────┘
```

---

## 3. URL 检测

### 3.1 支持的 URL 模式

**小红书**：

```python
_XHS_PATTERNS = [
    # 标准链接
    re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(\w+)"),
    # 短链接
    re.compile(r"(?:https?://)?xhslink\.com/(\w+)"),
]
```

**微博**：

```python
_WEIBO_PATTERNS = [
    # PC 端
    re.compile(r"(?:https?://)?(?:www\.)?weibo\.com/(?:detail/(\d+)|(\d+/\w+))"),
    # 移动端
    re.compile(r"(?:https?://)?m\.weibo\.cn/(?:status|detail)/(\d+)"),
]
```

### 3.2 detect_social_url()

```python
def detect_social_url(text: str) -> tuple[SocialPlatform | None, str]:
```

从任意文本中搜索第一个匹配的社交媒体 URL，返回 `(platform, url)` 或 `(None, "")`。

---

## 4. SocialFetcher — 内容抓取

### 4.1 类设计

```python
class SocialFetcher:
    def __init__(
        self,
        cookies_xhs: str = "",       # 小红书 Cookie（key=val; key=val 格式）
        cookies_weibo: str = "",     # 微博 Cookie
        fetch_timeout: int = 60,     # 超时秒数
    ) -> None
```

### 4.2 浏览器启动（Playwright + 反检测）

```python
async def start(self) -> None:
```

**启动配置**：

| 参数 | 值 | 说明 |
|------|-----|------|
| 浏览器 | Chromium headless | 无头模式 |
| `--no-sandbox` | 启用 | ECS/Docker 环境 |
| `--disable-dev-shm-usage` | 启用 | 避免 /dev/shm 内存不足 |
| `--disable-blink-features=AutomationControlled` | 启用 | 隐藏自动化特征 |
| playwright-stealth | 可选导入 | 补丁自动应用，增强反检测 |

**生命周期**：
- `start()` 在 `main.py` 启动时调用一次，长驻运行
- `shutdown()` 在应用关闭时调用
- 支持 `async with` 上下文管理器

### 4.3 Cookie 管理

Cookie 通过环境变量传入，以 `key1=val1; key2=val2` 格式存储：

```python
_parse_cookie_string("web_session=abc123; x2appId=devices_id")
# → [{"name": "web_session", "value": "abc123", "domain": "", "path": "/"}, ...]
```

注入时按平台设置 domain：
- 小红书：`.xiaohongshu.com`
- 微博：`.weibo.com`

### 4.4 小红书抓取流程（_fetch_xiaohongshu）

```
1. 短链解析（xhslink.com）
   └─ httpx GET（follow_redirects=True, max_redirects=5）
   └─ 获取最终 URL → 替换为 xiaohongshu.com/explore/xxx

2. 创建浏览器上下文
   └─ Viewport: 1280×800
   └─ User-Agent: Chrome/131 macOS
   └─ Cookie 注入（.xiaohongshu.com domain）

3. 导航 + 重定向循环检测
   └─ page.goto(url, wait_until="domcontentloaded")
   └─ framenavigated 事件计数器，nav_count > 10 → 重定向循环
   └─ 错误提示: "XHS cookie may be expired. Please update SOCIAL_XHS_COOKIE"

4. 等待内容渲染
   └─ wait_for_selector("#detail-desc", timeout=15000)
   └─ Fallback: ".note-content"

5. DOM 提取
   ├─ 标题: #detail-title
   ├─ 正文: #detail-desc（或 .note-text）
   ├─ 标签: #detail-desc a[href*='tag'] → 去掉 # 前缀
   ├─ 互动数据: .interact-button .count → {likes, collects, comments}
   ├─ 作者: .username 或 .author .name
   └─ 图片: .swiper-slide img, .note-image img（≤20张）

6. 图片下载
   └─ 通过 page.evaluate(fetch()) 下载（继承浏览器 Cookie）
   └─ 转 base64 存入 SocialImage
```

### 4.5 微博抓取流程（_fetch_weibo）

**双引擎策略**：

```
Path 1: JSON API（优先）
  └─ GET https://m.weibo.cn/statuses/show?id={weibo_id}
  └─ User-Agent: iPhone Safari
  └─ Cookie 注入
  └─ 解析 JSON → text/user/pics/interaction
  └─ 成功 → 返回

Path 2: Playwright（Fallback）
  └─ 创建移动端上下文（iPhone UA）
  └─ 导航到 m.weibo.cn/status/{id}
  └─ wait_until="networkidle"
  └─ DOM 提取: .weibo-text / .card-text / .WB_text
```

**JSON API 数据提取**：

| 字段 | JSON 路径 | 处理 |
|------|----------|------|
| 正文 | `data.text` | 去除 HTML 标签 |
| 作者 | `data.user.screen_name` | — |
| 时间 | `data.created_at` | — |
| 标签 | 正则 `#([^#]+)#` | 从原始 HTML 文本提取 |
| 图片 | `data.pics[].url` | — |
| 互动 | `attitudes_count` / `comments_count` / `reposts_count` | — |

### 4.6 SocialContent 数据结构

```python
SocialContent:
    url: str
    platform: SocialPlatform          # XIAOHONGSHU / WEIBO
    fetch_status: FetchStatus         # DONE / FAILED
    title: str                        # 帖子标题
    text: str                         # 正文内容
    author_name: str                  # 作者名称
    publish_time: str                 # 发布时间
    tags: list[str]                   # 标签列表
    images: list[SocialImage]         # 图片列表（含 base64）
    interaction: dict                 # {likes, comments, reposts, ...}
    error: str                        # 错误信息（FAILED 时）
```

---

## 5. OCR 双引擎策略

### 5.1 ImageOCRExtractor

```python
class ImageOCRExtractor:
    def __init__(
        self,
        dashscope_api_key: str = "",  # DashScope Key（qwen-vl-max 用）
        paddle_enabled: bool = True,  # 是否启用 PaddleOCR
    ) -> None
```

### 5.2 双引擎决策流程

```
extract(image_base64)
    │
    ▼
┌────────────────────────────┐
│ Engine 1: PaddleOCR (免费)  │
│  ├─ 本地推理，零 API 成本    │
│  ├─ lang="ch", use_angle_cls│
│  └─ asyncio.to_thread()     │
└────────────┬───────────────┘
             │
     置信度 ≥ 0.6 且 文本长度 > 10？
     ├─ 是 → 返回 PaddleOCR 结果 ✓
     │
     └─ 否（低置信度或失败）
         ▼
┌────────────────────────────┐
│ Engine 2: qwen-vl-max      │
│  ├─ DashScope API（付费）   │
│  ├─ 表格/代码/图表保留格式  │
│  ├─ max_tokens=2000         │
│  └─ temperature=0.1         │
└────────────┬───────────────┘
             │
     DashScope Key 可用？
     ├─ 是 → 返回 qwen-vl-max 结果 ✓
     │
     └─ 否 → 返回 PaddleOCR 结果（低置信度也算）
         或 返回空结果
```

### 5.3 PaddleOCR 结果解析

PaddleOCR 返回格式：`[[[bbox, (text, confidence)], ...]]`

```python
def _format_paddle_result(self, result) -> tuple[str, float]:
    """提取文本行并计算平均置信度"""
    lines = []
    confidences = []
    for block in result[0]:
        text = block[1][0]
        conf = block[1][1]
        if text.strip():
            lines.append(text)
            confidences.append(conf)
    avg_conf = sum(confidences) / len(confidences)
    return "\n".join(lines), avg_conf
```

### 5.4 qwen-vl-max Prompt

```
请提取这张图片中的所有文字内容。
- 如果是表格，保留表格结构
- 如果是代码，保留代码格式
- 如果有图表标注，也请提取
- 直接输出文字，不要添加解释
```

### 5.5 批量 OCR

```python
async def extract_batch(self, images_base64: list[str]) -> list[OCRResult]:
    """并行提取多张图片"""
    tasks = [self.extract(img) for img in images_base64]
    return await asyncio.gather(*tasks)
```

### 5.6 置信度阈值

```python
_PADDLE_CONFIDENCE_THRESHOLD = 0.6
```

低于此阈值的 PaddleOCR 结果会触发 qwen-vl-max fallback。该阈值基于实际测试：
- 中文清晰文本通常 > 0.85
- 手写/模糊/艺术字通常 < 0.5
- 0.6 是平衡准确率和 API 成本的折中值

---

## 6. 配置项

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `SOCIAL_XHS_COOKIE` | 小红书 Cookie（`key=val; key=val`） | 空 |
| `SOCIAL_WEIBO_COOKIE` | 微博 Cookie | 空 |
| `SOCIAL_FETCH_TIMEOUT` | 抓取超时（秒） | 60 |
| `DASHSCOPE_API_KEY` | DashScope API Key（OCR fallback） | 空 |
| `OCR_PADDLE_ENABLED` | 是否启用 PaddleOCR | true |

---

## 7. 错误处理

### 7.1 小红书常见错误

| 错误场景 | 检测方式 | 错误提示 |
|---------|---------|---------|
| Cookie 过期 | nav_count > 10（重定向循环） | "XHS cookie may be expired" |
| 页面加载超时 | `page.goto` TimeoutError | "Timeout after {ms}" |
| 内容选择器缺失 | `wait_for_selector` 失败 | 尝试备选选择器 |

### 7.2 微博常见错误

| 错误场景 | 处理 |
|---------|------|
| JSON API 返回 ok≠1 | 自动切换到 Playwright |
| API 响应非 JSON | 返回 None，触发 Playwright |
| Playwright 也失败 | 返回 FAILED + 错误信息 |

### 7.3 OCR 错误降级

```
PaddleOCR 失败 → qwen-vl-max
qwen-vl-max 失败 → 重试 PaddleOCR（即使低置信度）
全部失败 → 返回空文本
```

---

## 8. 依赖安装

```bash
# Playwright + 反检测
pip install playwright playwright-stealth
playwright install chromium

# PaddleOCR（可选，但推荐）
pip install paddlepaddle paddleocr
```

**注意**：PaddleOCR 未安装时，系统自动降级为 qwen-vl-max 付费模式，功能不受影响但产生 API 费用。

---

## 9. 影响文件清单

| 文件 | 行数 | 角色 |
|------|------|------|
| `kb/app/services/social_fetcher.py` | 597 | 社交媒体抓取核心（小红书/微博） |
| `kb/app/ingest/ocr.py` | 214 | OCR 双引擎（PaddleOCR + qwen-vl-max） |
| `kb/app/models.py` | — | SocialContent / SocialImage / OCRResult 数据模型 |
| `kb/app/feishu/handlers.py` | — | URL 检测 + 抓取路由集成 |
| `kb/app/config.py` | 143 | Cookie / OCR 配置项 |
