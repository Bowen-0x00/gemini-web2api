"""End-to-end integration test for the Gemini Web2API proxy server."""
import os
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"

import asyncio
import json
import logging
import sys
import unittest
import httpx
import uvicorn
from tests.mock_browser import MockBrowser

logger = logging.getLogger("test_e2e")
SERVER_PORT = 18765
BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"
WS_URL = f"ws://127.0.0.1:{SERVER_PORT}/ws"

class E2ETest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Configure uvicorn server in background
        config = uvicorn.Config("server:app", host="127.0.0.1", port=SERVER_PORT, log_level="warning")
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())

        # Wait for server ready
        for _ in range(50):
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    r = await client.get(f"{BASE_URL}/v1/models")
                    if r.status_code == 200:
                        break
            except Exception:
                await asyncio.sleep(0.1)

        # Start mock browser
        self.mock_browser = MockBrowser(ws_url=WS_URL, model="gemini-4-argon")
        self.mock_task = asyncio.create_task(self.mock_browser.run())
        await asyncio.sleep(0.5)

    async def asyncTearDown(self):
        self.mock_browser.running = False
        self.mock_task.cancel()
        self.server.should_exit = True
        await asyncio.sleep(0.2)

    async def test_01_models_endpoint(self):
        """Test GET /v1/models returns model list."""
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(f"{BASE_URL}/v1/models")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            model_ids = [m["id"] for m in data["data"]]
            self.assertIn("gemini-4-argon", model_ids)

    async def test_02_simple_chat_completion_streaming(self):
        """Test streaming chat completion without tools."""
        payload = {
            "model": "gemini-4-argon",
            "messages": [
                {"role": "user", "content": "Introduce yourself."}
            ],
            "stream": True,
            "user": "test_user_1"
        }

        collected_content = []
        finish_reason = None

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            async with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
                self.assertEqual(resp.status_code, 200)
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"]
                        if "content" in delta and delta["content"]:
                            collected_content.append(delta["content"])
                        if chunk["choices"][0].get("finish_reason"):
                            finish_reason = chunk["choices"][0]["finish_reason"]

        full_text = "".join(collected_content)
        self.assertIn("Gemini 4 Argon", full_text)
        self.assertEqual(finish_reason, "stop")

    async def test_03_tool_calling_and_context_diffing(self):
        """Test multi-turn tool calling and context diffing."""
        tools = [{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"}
                    },
                    "required": ["command"]
                }
            }
        }]

        # Turn 1: Agent asks model to run command
        messages = [
            {"role": "user", "content": "Run bash command to check user"}
        ]
        payload = {
            "model": "gemini-4-argon",
            "messages": messages,
            "tools": tools,
            "stream": True,
            "user": "multi_turn_session"
        }

        tool_calls = []
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            async with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=payload) as resp:
                self.assertEqual(resp.status_code, 200)
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"]
                        if "tool_calls" in delta and delta["tool_calls"]:
                            tool_calls.extend(delta["tool_calls"])

        self.assertTrue(len(tool_calls) > 0)
        call = tool_calls[0]
        self.assertEqual(call["function"]["name"], "bash")
        args = json.loads(call["function"]["arguments"])
        self.assertEqual(args.get("command"), "whoami")

        # Turn 2: Agent executes bash, appends tool result, calls API again
        messages.append({
            "role": "assistant",
            "tool_calls": tool_calls
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "name": "bash",
            "content": "developer_user"
        })

        payload2 = {
            "model": "gemini-4-argon",
            "messages": messages,
            "tools": tools,
            "stream": True,
            "user": "multi_turn_session"
        }

        turn2_content = []
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            async with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=payload2) as resp:
                self.assertEqual(resp.status_code, 200)
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"]
                        if "content" in delta and delta["content"]:
                            turn2_content.append(delta["content"])

        turn2_text = "".join(turn2_content)
        self.assertIn("The current user is verified", turn2_text)

        # Verify Context Differ behavior: Turn 2 should only have sent the incremental tool result to the browser!
        # Check mock browser history:
        # Prompt 1 should contain "=== System Instructions ===" and "Available Tools"
        self.assertIn("Available Tools", self.mock_browser.history[0])
        # Prompt 2 should ONLY contain the tool result delta, NOT the entire system instructions again!
        self.assertNotIn("=== System Instructions ===", self.mock_browser.history[1])
        self.assertIn("Tool Result (`bash`)", self.mock_browser.history[1])
        self.assertIn("developer_user", self.mock_browser.history[1])

    async def test_04_anthropic_messages_endpoint(self):
        """Test Anthropic /v1/messages compatibility."""
        payload = {
            "model": "gemini-4-argon",
            "messages": [
                {"role": "user", "content": "Hello from Anthropic client!"}
            ],
            "stream": True
        }

        collected_text = []
        stop_reason = None

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            async with client.stream("POST", f"{BASE_URL}/v1/messages", json=payload) as resp:
                self.assertEqual(resp.status_code, 200)
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("type") == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                collected_text.append(delta.get("text", ""))
                        elif data.get("type") == "message_delta":
                            stop_reason = data.get("delta", {}).get("stop_reason")

        full_text = "".join(collected_text)
        self.assertIn("Gemini 4 Argon", full_text)
        self.assertEqual(stop_reason, "end_turn")

if __name__ == "__main__":
    unittest.main()
