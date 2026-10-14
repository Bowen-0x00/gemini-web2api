"""Mock browser WebSocket client for automated testing of the Gemini Web2API proxy."""

import asyncio
import json
import logging
import sys

logger = logging.getLogger("mock_browser")

class MockBrowser:
    def __init__(self, ws_url: str = "ws://127.0.0.1:8765/ws", model: str = "gemini-4-argon"):
        self.ws_url = ws_url
        self.model = model
        self.running = True
        self.history = []

    async def run(self):
        import websockets
        logger.info(f"Connecting to {self.ws_url}...")
        async with websockets.connect(self.ws_url) as ws:
            # Register
            await ws.send(json.dumps({
                "type": "register",
                "tab_id": "mock_browser_tab",
                "model": self.model,
                "url": "https://gemini.google.com/app"
            }))
            reg_resp = await ws.recv()
            logger.info(f"Registered: {reg_resp}")

            while self.running:
                try:
                    msg_raw = await ws.recv()
                    msg = json.loads(msg_raw)
                    msg_type = msg.get("type")

                    if msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                        continue

                    if msg_type == "chat_request":
                        req_id = msg.get("request_id")
                        text = msg.get("text", "")
                        self.history.append(text)
                        logger.info(f"Mock received prompt for {req_id}: {text[:80]}...")

                        # Respond based on prompt content
                        if "bash" in text and "Tool Result" not in text:
                            # Model decides to call bash tool
                            chunks = [
                                "Let me examine the environment.\n",
                                "<tool_call>\n",
                                "{\n  \"name\": \"bash\",\n",
                                "  \"arguments\": {\"command\": \"whoami\"}\n",
                                "}\n</tool_call>",
                                "\nSimulated trailing text that should be cut off."
                            ]
                        elif "Tool Result" in text:
                            # Response after receiving tool result
                            chunks = [
                                "I see the command output: ",
                                "The current user is verified. ",
                                "Everything looks great!"
                            ]
                        else:
                            # Normal response
                            chunks = [
                                "Hello! I am ",
                                "Gemini 4 Argon, ",
                                "ready to assist you."
                            ]

                        # Stream chunks with small delays
                        for c in chunks:
                            await ws.send(json.dumps({
                                "type": "delta",
                                "request_id": req_id,
                                "chunk": c
                            }))
                            await asyncio.sleep(0.05)

                        # Finish
                        await ws.send(json.dumps({
                            "type": "done",
                            "request_id": req_id
                        }))

                    elif msg_type == "abort_generation":
                        logger.info(f"Mock browser received abort_generation signal for {msg.get('request_id')}")

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in mock loop: {e}")
                    break

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mock = MockBrowser()
    asyncio.run(mock.run())
