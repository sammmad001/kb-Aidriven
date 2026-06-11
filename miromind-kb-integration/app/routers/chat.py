"""
核心聊天路由 - 流式代理 MiroMind API
集成 stream_recorder 实现消息持久化

变更说明（个人知识库集成）：
- 在创建 StreamRecorder 前查询会话标题和模型
- 将 session_title / session_model 传递给 StreamRecorder
"""
import json
import uuid

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from aiosqlite import Connection

from app.config import MIROMIND_API_BASE, MIROMIND_API_KEY, DEFAULT_MODEL, REQUEST_TIMEOUT, SEARCH_INSTRUCTION
from app.auth import require_user
from app.database import get_db
from app.models import ChatRequest, CancelRequest
from app.services.stream_recorder import StreamRecorder

router = APIRouter(tags=["chat"])

# 存储活跃请求的 response_id，用于取消
active_requests: dict[str, str] = {}


@router.post("/api/chat")
async def chat(
    body: ChatRequest,
    user: dict = Depends(require_user),
    db: Connection = Depends(get_db),
):
    """流式代理 MiroMind Responses API，同时记录消息"""
    if isinstance(user, JSONResponse):
        return user

    user_message = body.message.strip()
    model = body.model or DEFAULT_MODEL
    session_id = body.session_id
    use_responses_api = body.use_responses

    if not user_message:
        return JSONResponse(status_code=400, content={"error": "消息不能为空"})

    # 如果没有 session_id，自动创建一个新会话
    if not session_id:
        # 用用户消息前 30 字符作为标题
        title = user_message[:30].replace("\n", " ")
        if len(user_message) > 30:
            title += "..."
        cursor = await db.execute(
            "INSERT INTO sessions (user_id, title, model) VALUES (?, ?, ?)",
            (user["id"], title, model)
        )
        await db.commit()
        session_id = cursor.lastrowid
    else:
        # 验证会话属于当前用户
        cursor = await db.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ? AND is_deleted = 0",
            (session_id, user["id"])
        )
        if not await cursor.fetchone():
            return JSONResponse(status_code=404, content={"error": "会话不存在"})

    # ── 知识库集成：查询会话标题和模型，供 KB 导入使用 ──
    session_title = "新对话"
    session_model = model
    if session_id:
        cursor = await db.execute(
            "SELECT title, model FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if row:
            session_title = row["title"]
            session_model = row["model"]

    request_id = str(uuid.uuid4())[:8]

    # 注入搜索引导指令
    if SEARCH_INSTRUCTION:
        enriched_message = f"{SEARCH_INSTRUCTION}\n\n{user_message}"
    else:
        enriched_message = user_message

    # 选择流式生成器
    if use_responses_api:
        inner_stream = _stream_responses_api(enriched_message, model, request_id)
    else:
        inner_stream = _stream_chat_api(enriched_message, model, request_id)

    # 使用 StreamRecorder 包装（传入会话标题和模型）
    recorder = StreamRecorder(
        inner=inner_stream,
        db=db,
        user_id=user["id"],
        session_id=session_id,
        user_message=user_message,
        model=model,
        session_title=session_title,
        session_model=session_model,
    )

    return StreamingResponse(
        recorder.record(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": str(session_id),
        },
    )


@router.post("/api/cancel")
async def cancel_task(request_body: CancelRequest):
    """取消正在进行的任务"""
    request_id = request_body.request_id
    response_id = active_requests.get(request_id)
    if not response_id:
        return {"ok": False, "error": "无活跃任务"}

    headers = {
        "Authorization": f"Bearer {MIROMIND_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                f"{MIROMIND_API_BASE}/responses/{response_id}/cancel",
                headers=headers,
            )
            return {"ok": True, "status": resp.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}


@router.get("/api/health")
async def health():
    return {"status": "ok", "model": DEFAULT_MODEL, "version": "3.0"}


@router.get("/api/models")
async def list_models():
    """返回可用模型列表"""
    from app.config import MODELS
    return {"models": MODELS}


# ============ 内部流式生成器 ============

async def _stream_responses_api(user_message: str, model: str, request_id: str):
    """使用 Responses API（推荐）"""
    payload = {
        "model": model,
        "input": user_message,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {MIROMIND_API_KEY}",
        "Content-Type": "application/json",
    }

    yield f"data: {json.dumps({'type': 'start', 'request_id': request_id}, ensure_ascii=False)}\n\n"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT)) as client:
            async with client.stream(
                "POST",
                f"{MIROMIND_API_BASE}/responses",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    yield f"data: {json.dumps({'type': 'error', 'content': f'HTTP {resp.status_code}: {error_text.decode()[:200]}'}, ensure_ascii=False)}\n\n"
                    return

                response_id = None
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    if line.startswith(":"):
                        yield ": heartbeat\n\n"
                        continue
                    if line.startswith("event: "):
                        continue
                    if not line.startswith("data: "):
                        continue

                    raw = line[6:]
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    evt_type = evt.get("type", "")

                    if evt_type == "response.created":
                        resp_obj = evt.get("response", {})
                        response_id = resp_obj.get("id", "")
                        if response_id:
                            active_requests[request_id] = response_id
                        continue

                    if evt_type == "response.reasoning_text.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            yield f"data: {json.dumps({'type': 'thinking', 'content': delta}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.output_item.added":
                        item = evt.get("item", {})
                        item_type = item.get("type", "")
                        if item_type == "tool_call":
                            yield f"data: {json.dumps({'type': 'tool_start', 'name': item.get('name', '')}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.output_item.done":
                        item = evt.get("item", {})
                        item_type = item.get("type", "")
                        if item_type == "tool_call":
                            tool_name = item.get("name", "")
                            arguments = item.get("arguments", {})
                            result = item.get("result", "")
                            yield f"data: {json.dumps({'type': 'tool_done', 'name': tool_name, 'arguments': arguments, 'result': result[:500] if isinstance(result, str) else str(result)[:500]}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            yield f"data: {json.dumps({'type': 'content', 'content': delta}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.completed":
                        resp_obj = evt.get("response", {})
                        usage = resp_obj.get("usage", {})
                        status = resp_obj.get("status", "completed")
                        yield f"data: {json.dumps({'type': 'done', 'finish_reason': status, 'usage': usage, 'response_id': response_id}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.failed":
                        error = evt.get("error", {})
                        yield f"data: {json.dumps({'type': 'error', 'content': str(error)}, ensure_ascii=False)}\n\n"

                    elif evt_type == "response.web_search_call.completed":
                        yield f"data: {json.dumps({'type': 'search_done', 'data': evt}, ensure_ascii=False)}\n\n"

    except httpx.ReadTimeout:
        yield f"data: {json.dumps({'type': 'error', 'content': '请求超时（300秒），请尝试简化问题'}, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
    finally:
        active_requests.pop(request_id, None)


async def _stream_chat_api(user_message: str, model: str, request_id: str):
    """降级使用 Chat Completions API"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": user_message}],
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {MIROMIND_API_KEY}",
        "Content-Type": "application/json",
    }

    yield f"data: {json.dumps({'type': 'start', 'request_id': request_id}, ensure_ascii=False)}\n\n"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT)) as client:
            async with client.stream(
                "POST",
                f"{MIROMIND_API_BASE}/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    yield f"data: {json.dumps({'type': 'error', 'content': error_text.decode()[:200]}, ensure_ascii=False)}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if line.startswith(":"):
                        yield ": heartbeat\n\n"
                        continue
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        yield "data: [DONE]\n\n"
                        return
                    try:
                        chunk = json.loads(raw)
                        choice = chunk.get("choices", [{}])[0]
                        delta = choice.get("delta", {})

                        reasoning_steps = delta.get("reasoning_steps", [])
                        for step in reasoning_steps:
                            step_type = step.get("type", "")
                            if step_type == "thinking":
                                yield f"data: {json.dumps({'type': 'thinking', 'content': step.get('thought', '')}, ensure_ascii=False)}\n\n"
                            elif step_type == "web_search":
                                ws = step.get("web_search", {})
                                yield f"data: {json.dumps({'type': 'search', 'keywords': ws.get('search_keywords', []), 'results': ws.get('search_results', [])}, ensure_ascii=False)}\n\n"
                            elif step_type == "fetch_url_content":
                                yield f"data: {json.dumps({'type': 'fetch', 'content': step.get('url', '')}, ensure_ascii=False)}\n\n"
                            elif step_type == "execute_python":
                                yield f"data: {json.dumps({'type': 'python', 'content': step.get('code', '')}, ensure_ascii=False)}\n\n"
                            elif step_type == "execute_command":
                                yield f"data: {json.dumps({'type': 'command', 'content': step.get('command', '')}, ensure_ascii=False)}\n\n"
                            elif step_type == "tool_call":
                                yield f"data: {json.dumps({'type': 'tool_call', 'name': step.get('name', ''), 'arguments': step.get('arguments', {})}, ensure_ascii=False)}\n\n"
                            else:
                                yield f"data: {json.dumps({'type': 'step', 'step_type': step_type, 'content': step}, ensure_ascii=False)}\n\n"

                        content = delta.get("content", "")
                        if content:
                            yield f"data: {json.dumps({'type': 'content', 'content': content}, ensure_ascii=False)}\n\n"

                        finish_reason = choice.get("finish_reason")
                        if finish_reason:
                            usage = chunk.get("usage", {})
                            if finish_reason == "error":
                                error_obj = choice.get("error", {})
                                yield f"data: {json.dumps({'type': 'error', 'content': str(error_obj)}, ensure_ascii=False)}\n\n"
                            else:
                                yield f"data: {json.dumps({'type': 'done', 'finish_reason': finish_reason, 'usage': usage}, ensure_ascii=False)}\n\n"

                    except json.JSONDecodeError:
                        continue
    except httpx.ReadTimeout:
        yield f"data: {json.dumps({'type': 'error', 'content': '请求超时（300秒），请尝试简化问题'}, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
    finally:
        active_requests.pop(request_id, None)
