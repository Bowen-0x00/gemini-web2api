"""FastAPI OpenAI-compatible reverse proxy server bridging CLI agents to browser models."""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from bridge.context_differ import ContextDiffer
from bridge.stream_parser import StreamParser
from bridge.ws_manager import WSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gemini_web_proxy")

app = FastAPI(title="Gemini Web2API Reverse Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ws_manager = WSManager()
differ = ContextDiffer()


class ChatCompletionRequest(BaseModel):
    model: str = "gemini-4-argon"
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    user: Optional[str] = None

class AnthropicMessagesRequest(BaseModel):
    model: str = "gemini-4-argon"
    messages: List[Dict[str, Any]]
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    stream: Optional[bool] = False
    max_tokens: Optional[int] = 4096


def normalize_anthropic_to_openai(req: AnthropicMessagesRequest) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """Normalize Anthropic messages and tools format to standard OpenAI representation."""
    norm_messages: List[Dict[str, Any]] = []

    if req.system:
        if isinstance(req.system, str):
            norm_messages.append({"role": "system", "content": req.system})
        elif isinstance(req.system, list):
            sys_text = "\n".join(b.get("text", "") for b in req.system if isinstance(b, dict))
            norm_messages.append({"role": "system", "content": sys_text})

    for m in req.messages:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "tool_result":
                            res_content = block.get("content", "")
                            if isinstance(res_content, list):
                                res_content = "\n".join(rb.get("text", "") for rb in res_content if isinstance(rb, dict))
                            norm_messages.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": res_content
                            })
                        elif btype == "text":
                            text_parts.append(block.get("text", ""))
                if text_parts:
                    norm_messages.append({"role": "user", "content": "\n".join(text_parts)})
            else:
                norm_messages.append({"role": "user", "content": str(content) if content is not None else ""})

        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                                }
                            })
                msg_dict: Dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                norm_messages.append(msg_dict)
            else:
                norm_messages.append({"role": "assistant", "content": str(content) if content is not None else ""})

    norm_tools = None
    if req.tools:
        norm_tools = []
        for t in req.tools:
            norm_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", "tool"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {})
                }
            })

    return norm_messages, norm_tools



@app.get("/")
async def index():
    """Simple status dashboard."""
    tabs_html = ""
    for tid, tab in ws_manager.tabs.items():
        tabs_html += f"<li><b>{tab.model}</b> (Tab ID: <code>{tid}</code>) - Status: <span style='color:green'>{tab.status}</span> - URL: {tab.url}</li>"
    if not tabs_html:
        tabs_html = "<li><i>No browser tabs connected yet. Install the Tampermonkey script and open Gemini Web.</i></li>"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Gemini Web2API Proxy</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; line-height: 1.5; }}
            h1 {{ color: #1a73e8; }}
            .card {{ border: 1px solid #e0e0e0; border-radius: 8px; padding: 1.2rem; margin-bottom: 1.5rem; }}
            code {{ background: #f1f3f4; padding: 2px 6px; border-radius: 4px; }}
            .status {{ font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>Gemini Web2API Reverse Proxy</h1>
        <p>Bridges CLI Agents (like <code>oh-my-pi</code>) to Gemini Web via browser automation.</p>
        
        <div class="card">
            <h3>Connected Browser Tabs ({len(ws_manager.tabs)})</h3>
            <ul>{tabs_html}</ul>
        </div>

        <div class="card">
            <h3>Active Sessions ({len(differ.sessions)})</h3>
            <p>Active conversation sessions: {len(differ.sessions)}</p>
        </div>

        <div class="card">
            <h3>Usage Instructions</h3>
            <p>1. Ensure Tampermonkey script is installed and Gemini tab is open.</p>
            <p>2. Set environment variables for your CLI agent:</p>
            <pre><code>export OPENAI_BASE_URL="http://127.0.0.1:8765/v1"
export OPENAI_API_KEY="dummy"
omp --model openai/gemini-4-argon "Hello, who are you?"</code></pre>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/v1/models")
async def list_models():
    """List available models for OpenAI compatibility."""
    # List model IDs including gemini-4-argon, gpt-4o, etc.
    models = [
        {"id": "gemini-4-argon", "object": "model", "owned_by": "google"},
        {"id": "gemini-4-argon:high", "object": "model", "owned_by": "google"},
        {"id": "gemini-4-argon:medium", "object": "model", "owned_by": "google"},
        {"id": "gemini-4-argon:low", "object": "model", "owned_by": "google"},
        {"id": "gemini-4-argon:off", "object": "model", "owned_by": "google"},
        {"id": "gemini-2.5-pro", "object": "model", "owned_by": "google"},
        {"id": "gemini-2.5-flash", "object": "model", "owned_by": "google"},
        {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
        {"id": "claude-3-5-sonnet", "object": "model", "owned_by": "anthropic"},
    ]
    return {"object": "list", "data": models}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    tab_id = f"tab_{uuid.uuid4().hex[:8]}"
    model = "gemini-4-argon"
    url = "unknown"

    try:
        # Await registration message
        init_data = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        if init_data.get("type") == "register":
            tab_id = init_data.get("tab_id", tab_id)
            model = init_data.get("model", model)
            url = init_data.get("url", url)

        tab = await ws_manager.register(tab_id=tab_id, model=model, url=url, ws=websocket, mode="ws")
        await websocket.send_json({"type": "registered", "tab_id": tab_id, "status": "ok"})

        while True:
            data = await websocket.receive_json()
            await ws_manager.handle_browser_message(tab_id, data)

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {tab_id}")
    except asyncio.TimeoutError:
        logger.warning(f"Registration timeout for connection")
    except Exception as e:
        logger.error(f"WebSocket error on tab {tab_id}: {e}")
    finally:
        ws_manager.unregister(tab_id)


@app.post("/browser/register")
async def browser_register(request: Request):
    """Register a browser tab using HTTP long-polling (bypasses CSP)."""
    data = await request.json()
    tab_id = data.get("tab_id", f"tab_{uuid.uuid4().hex[:8]}")
    model = data.get("model", "gemini-4-argon")
    url = data.get("url", "unknown")
    await ws_manager.register(tab_id=tab_id, model=model, url=url, ws=None, mode="http")
    return {"status": "ok", "tab_id": tab_id}


@app.get("/browser/poll")
async def browser_poll(tab_id: str, timeout: float = 30.0):
    """Long-poll endpoint for browser tabs to receive prompts."""
    tab = ws_manager.tabs.get(tab_id)
    if not tab:
        return {"type": "re_register"}
    tab.touch()
    try:
        task = await asyncio.wait_for(tab.pending_tasks.get(), timeout=timeout)
        return task
    except asyncio.TimeoutError:
        return {"type": "idle"}


@app.post("/browser/chunk")
async def browser_chunk(request: Request):
    """Receive streaming chunk from browser tab."""
    data = await request.json()
    tab_id = data.get("tab_id")
    req_id = data.get("request_id")
    chunk = data.get("chunk", "")

    tab = ws_manager.tabs.get(tab_id)
    if tab:
        tab.touch()

    if req_id and req_id in ws_manager.active_requests:
        await ws_manager.active_requests[req_id].put({
            "type": "delta",
            "request_id": req_id,
            "chunk": chunk
        })

    should_abort = False
    if tab and req_id in tab.abort_flags:
        should_abort = True

    return {"status": "ok", "abort": should_abort}


@app.post("/browser/done")
async def browser_done(request: Request):
    """Signal completion of generation from browser tab."""
    data = await request.json()
    tab_id = data.get("tab_id")
    req_id = data.get("request_id")

    tab = ws_manager.tabs.get(tab_id)
    if tab:
        tab.touch()

    if req_id and req_id in ws_manager.active_requests:
        await ws_manager.active_requests[req_id].put({
            "type": "done",
            "request_id": req_id
        })
    return {"status": "ok"}


@app.post("/browser/error")
async def browser_error(request: Request):
    """Report error from browser tab."""
    data = await request.json()
    tab_id = data.get("tab_id")
    req_id = data.get("request_id")
    message = data.get("message", "Unknown error from browser")

    tab = ws_manager.tabs.get(tab_id)
    if tab:
        tab.touch()

    if req_id and req_id in ws_manager.active_requests:
        await ws_manager.active_requests[req_id].put({
            "type": "error",
            "request_id": req_id,
            "message": message
        })
    return {"status": "ok"}


@app.get("/browser/heartbeat")
async def browser_heartbeat(tab_id: str):
    """Heartbeat to keep tab alive."""
    tab = ws_manager.tabs.get(tab_id)
    if tab:
        tab.touch()
        return {"status": "ok"}
    return {"status": "not_found"}

@app.post("/browser/log")
async def browser_log(request: Request):
    """Log debug messages sent from browser script."""
    data = await request.json()
    tab_id = data.get("tab_id", "unknown")
    message = data.get("message", "")
    logger.info(f"[Browser {tab_id}]: {message}")
    return {"status": "ok"}

def extract_thinking_level(model_name: str) -> Tuple[str, Optional[str]]:
    """Extract base model name and optional thinking level suffix (:high, :medium, :low, :off)."""
    if ":" in model_name:
        parts = model_name.split(":", 1)
        base = parts[0]
        suffix = parts[1].lower()
        if suffix in ["high", "medium", "low", "off"]:
            return base, suffix
    return model_name, None

def infer_session_id(request: Request, body: ChatCompletionRequest) -> str:
    """Infer or extract a stable session ID for context diffing."""
    # Check headers first
    header_sid = request.headers.get("x-session-id") or request.headers.get("conversation-id")
    if header_sid:
        return header_sid
    if body.user:
        return f"user_{body.user}"

    # Fallback: compute signature from the first user message
    for msg in body.messages:
        if msg.get("role") == "user":
            content = str(msg.get("content", ""))[:80]
            import hashlib
            return f"sess_{hashlib.md5(content.encode()).hexdigest()[:12]}"

    return "default_session"


async def generate_sse_stream(
    req_id: str,
    model: str,
    prompt_to_send: str,
    need_reset: bool,
    tab: Any,
    queue: asyncio.Queue,
    thinking_level: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Generates SSE chunks for streaming OpenAI completions."""
    parser = StreamParser()

    # Dispatch to browser
    payload = {
        "type": "chat_request",
        "request_id": req_id,
        "text": prompt_to_send,
        "need_reset": need_reset,
        "thinking_level": thinking_level,
    }
    sent = await ws_manager.send_to_tab(tab, payload)
    if not sent:
        yield f"data: {json.dumps({'error': {'message': 'Failed to send prompt to browser tab.'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    tab.status = "busy"
    ws_manager.request_tabs[req_id] = tab.tab_id

    try:
        created_ts = int(time.time())
        finished = False

        while not finished:
            try:
                # Wait for next event from browser with 3s timeout for keep-alive
                msg = await asyncio.wait_for(queue.get(), timeout=3.0)
            except asyncio.TimeoutError:
                # SSE keep-alive heartbeat to prevent client timeout
                yield ": keep-alive\n\n"
                continue

            msg_type = msg.get("type")

            if msg_type == "delta":
                chunk_text = msg.get("chunk", "")
                events = parser.feed(chunk_text)
                for ev in events:
                    if ev.event_type == "content":
                        chunk_obj = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": ev.content},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n"

                    elif ev.event_type == "tool_call":
                        tc = ev.tool_call
                        chunk_obj = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": 0,
                                        "id": tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": tc["function"]["name"],
                                            "arguments": tc["function"]["arguments"],
                                        },
                                    }],
                                },
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n"
                        # Trigger web stop button to stop further generation
                        await ws_manager.abort_generation(req_id)

            elif msg_type == "done":
                finished = True
                final_events = parser.finalize()
                for ev in final_events:
                    if ev.event_type == "content":
                        chunk_obj = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": ev.content},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n"
                    elif ev.event_type == "tool_call":
                        tc = ev.tool_call
                        chunk_obj = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": 0,
                                        "id": tc["id"],
                                        "type": "function",
                                        "function": {
                                            "name": tc["function"]["name"],
                                            "arguments": tc["function"]["arguments"],
                                        },
                                    }],
                                },
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n"
                    elif ev.event_type == "done":
                        finish_reason = ev.finish_reason or "stop"
                        chunk_obj = {
                            "id": f"chatcmpl-{req_id}",
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason,
                            }],
                        }
                        yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n"

            elif msg_type == "error":
                err_msg = msg.get("message", "Unknown error from browser bridge")
                logger.error(f"Error from browser: {err_msg}")
                yield f"data: {json.dumps({'error': {'message': err_msg}})}\n\n"
                finished = True

        yield "data: [DONE]\n\n"

    finally:
        ws_manager.cleanup_request(req_id)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """Handle OpenAI chat completions API calls."""
    session_id = infer_session_id(request, body)
    base_model, thinking_level = extract_thinking_level(body.model)
    logger.info(f"Incoming chat request for model '{body.model}' (base={base_model}, thinking={thinking_level}, session: {session_id}, stream={body.stream})")

    # Context diffing
    prompt_to_send, need_reset = differ.diff_and_compile(
        session_id=session_id,
        messages=body.messages,
        tools=body.tools,
    )

    if not prompt_to_send:
        # No new messages to send
        logger.info(f"No new message delta for session {session_id}")
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "No new messages received."},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        })

    # Find available browser tab
    tab = ws_manager.get_available_tab(preferred_model=base_model)
    if not tab:
        raise HTTPException(
            status_code=503,
            detail=(
                "No browser tab connected via WebSocket. "
                "Please open Gemini Web (or Google AI Studio) in your browser with the Tampermonkey script installed."
            )
        )

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    queue = ws_manager.create_request_channel(req_id)

    if body.stream:
        return StreamingResponse(
            generate_sse_stream(
                req_id=req_id,
                model=body.model,
                prompt_to_send=prompt_to_send,
                need_reset=need_reset,
                tab=tab,
                queue=queue,
                thinking_level=thinking_level,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        # Non-streaming mode: collect chunks
        parser = StreamParser()
        payload = {
            "type": "chat_request",
            "request_id": req_id,
            "text": prompt_to_send,
            "need_reset": need_reset,
            "thinking_level": thinking_level,
        }
        sent = await ws_manager.send_to_tab(tab, payload)
        if not sent:
            ws_manager.cleanup_request(req_id)
            raise HTTPException(status_code=500, detail="Failed to send prompt to browser tab.")

        tab.status = "busy"
        ws_manager.request_tabs[req_id] = tab.tab_id

        accumulated_content = []
        try:
            finished = False
            while not finished:
                msg = await queue.get()
                msg_type = msg.get("type")
                if msg_type == "delta":
                    events = parser.feed(msg.get("chunk", ""))
                    for ev in events:
                        if ev.event_type == "content":
                            accumulated_content.append(ev.content)
                        elif ev.event_type == "tool_call":
                            await ws_manager.abort_generation(req_id)
                elif msg_type == "done":
                    finished = True
                    events = parser.finalize()
                    for ev in events:
                        if ev.event_type == "content":
                            accumulated_content.append(ev.content)
                elif msg_type == "error":
                    raise HTTPException(status_code=500, detail=msg.get("message", "Error from browser"))

            full_text = "".join(accumulated_content)
            tool_calls = parser.parsed_tool_calls if parser.parsed_tool_calls else None
            finish_reason = "tool_calls" if tool_calls else "stop"

            return {
                "id": f"chatcmpl-{req_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": full_text if full_text else None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": finish_reason,
                }],
                "usage": {
                    "prompt_tokens": len(prompt_to_send) // 4,
                    "completion_tokens": len(full_text) // 4,
                    "total_tokens": (len(prompt_to_send) + len(full_text)) // 4,
                }
            }
        finally:
            ws_manager.cleanup_request(req_id)


@app.post("/v1/session/reset")
async def reset_session(session_id: Optional[str] = None):
    """Endpoint to explicitly reset conversation session state."""
    if session_id:
        differ.reset_session(session_id)
    else:
        differ.sessions.clear()
    return {"status": "ok", "message": "Session state cleared"}

async def generate_anthropic_sse_stream(
    req_id: str,
    model: str,
    prompt_to_send: str,
    need_reset: bool,
    tab: Any,
    queue: asyncio.Queue,
) -> AsyncGenerator[str, None]:
    """Generates SSE chunks according to the Anthropic Messages API format."""
    parser = StreamParser()
    msg_id = f"msg_{uuid.uuid4().hex[:14]}"

    payload = {
        "type": "chat_request",
        "request_id": req_id,
        "text": prompt_to_send,
        "need_reset": need_reset,
    }
    sent = await ws_manager.send_to_tab(tab, payload)
    if not sent:
        yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': 'Failed to send prompt to browser tab.'}})}\n\n"
        return

    tab.status = "busy"
    ws_manager.request_tabs[req_id] = tab.tab_id

    start_event = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": len(prompt_to_send) // 4, "output_tokens": 1}
        }
    }
    yield f"event: message_start\ndata: {json.dumps(start_event, ensure_ascii=False)}\n\n"

    text_block_started = False
    block_index = 0

    try:
        finished = False
        while not finished:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=3.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            msg_type = msg.get("type")

            if msg_type == "delta":
                chunk_text = msg.get("chunk", "")
                events = parser.feed(chunk_text)
                for ev in events:
                    if ev.event_type == "content":
                        if not text_block_started:
                            cb_start = {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {"type": "text", "text": ""}
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(cb_start, ensure_ascii=False)}\n\n"
                            text_block_started = True
                        cb_delta = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": ev.content}
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(cb_delta, ensure_ascii=False)}\n\n"
                    elif ev.event_type == "tool_call":
                        if text_block_started:
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                            block_index += 1
                            text_block_started = False
                        tc = ev.tool_call
                        func = tc["function"]
                        cb_start = {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": func["name"],
                                "input": {}
                            }
                        }
                        yield f"event: content_block_start\ndata: {json.dumps(cb_start, ensure_ascii=False)}\n\n"
                        cb_delta = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "input_json_delta", "partial_json": func["arguments"]}
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(cb_delta, ensure_ascii=False)}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                        block_index += 1
                        await ws_manager.abort_generation(req_id)

            elif msg_type == "done":
                finished = True
                final_events = parser.finalize()
                for ev in final_events:
                    if ev.event_type == "content":
                        if not text_block_started:
                            cb_start = {
                                "type": "content_block_start",
                                "index": block_index,
                                "content_block": {"type": "text", "text": ""}
                            }
                            yield f"event: content_block_start\ndata: {json.dumps(cb_start, ensure_ascii=False)}\n\n"
                            text_block_started = True
                        cb_delta = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": ev.content}
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(cb_delta, ensure_ascii=False)}\n\n"
                    elif ev.event_type == "tool_call":
                        if text_block_started:
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                            block_index += 1
                            text_block_started = False
                        tc = ev.tool_call
                        func = tc["function"]
                        cb_start = {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": func["name"],
                                "input": {}
                            }
                        }
                        yield f"event: content_block_start\ndata: {json.dumps(cb_start, ensure_ascii=False)}\n\n"
                        cb_delta = {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "input_json_delta", "partial_json": func["arguments"]}
                        }
                        yield f"event: content_block_delta\ndata: {json.dumps(cb_delta, ensure_ascii=False)}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                        block_index += 1

                if text_block_started:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"

                stop_reason = "tool_use" if parser.parsed_tool_calls else "end_turn"
                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 50}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

            elif msg_type == "error":
                err_msg = msg.get("message", "Error from browser")
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err_msg}})}\n\n"
                finished = True

    finally:
        ws_manager.cleanup_request(req_id)


@app.post("/v1/messages")
async def anthropic_messages(request: Request, body: AnthropicMessagesRequest):
    """Anthropic Messages API compatible endpoint."""
    norm_messages, norm_tools = normalize_anthropic_to_openai(body)

    pseudo_req = ChatCompletionRequest(
        model=body.model,
        messages=norm_messages,
        tools=norm_tools,
        stream=body.stream
    )
    session_id = infer_session_id(request, pseudo_req)
    logger.info(f"Incoming Anthropic Messages request (session: {session_id}, stream={body.stream})")

    prompt_to_send, need_reset = differ.diff_and_compile(
        session_id=session_id,
        messages=norm_messages,
        tools=norm_tools
    )

    tab = ws_manager.get_available_tab(preferred_model=body.model)
    if not tab:
        raise HTTPException(
            status_code=503,
            detail="No browser tab connected via WebSocket. Please open Gemini Web in your browser."
        )

    req_id = f"req_{uuid.uuid4().hex[:12]}"
    queue = ws_manager.create_request_channel(req_id)

    if body.stream:
        return StreamingResponse(
            generate_anthropic_sse_stream(
                req_id=req_id,
                model=body.model,
                prompt_to_send=prompt_to_send,
                need_reset=need_reset,
                tab=tab,
                queue=queue
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        parser = StreamParser()
        payload = {
            "type": "chat_request",
            "request_id": req_id,
            "text": prompt_to_send,
            "need_reset": need_reset,
        }
        sent = await ws_manager.send_to_tab(tab, payload)
        if not sent:
            ws_manager.cleanup_request(req_id)
            raise HTTPException(status_code=500, detail="Failed to send prompt to browser tab.")

        tab.status = "busy"
        ws_manager.request_tabs[req_id] = tab.tab_id

        try:
            finished = False
            accumulated_content = []
            while not finished:
                msg = await queue.get()
                mtype = msg.get("type")
                if mtype == "delta":
                    events = parser.feed(msg.get("chunk", ""))
                    for ev in events:
                        if ev.event_type == "content":
                            accumulated_content.append(ev.content)
                        elif ev.event_type == "tool_call":
                            await ws_manager.abort_generation(req_id)
                elif mtype == "done":
                    finished = True
                    events = parser.finalize()
                    for ev in events:
                        if ev.event_type == "content":
                            accumulated_content.append(ev.content)
                elif mtype == "error":
                    raise HTTPException(status_code=500, detail=msg.get("message", "Error from browser"))

            content_blocks = []
            text_res = "".join(accumulated_content)
            if text_res:
                content_blocks.append({"type": "text", "text": text_res})
            if parser.parsed_tool_calls:
                for tc in parser.parsed_tool_calls:
                    func = tc["function"]
                    args_dict = json.loads(func["arguments"]) if isinstance(func["arguments"], str) else func["arguments"]
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": func["name"],
                        "input": args_dict
                    })

            stop_reason = "tool_use" if parser.parsed_tool_calls else "end_turn"
            return {
                "id": f"msg_{uuid.uuid4().hex[:14]}",
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "model": body.model,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": len(prompt_to_send) // 4,
                    "output_tokens": len(text_res) // 4
                }
            }
        finally:
            ws_manager.cleanup_request(req_id)
