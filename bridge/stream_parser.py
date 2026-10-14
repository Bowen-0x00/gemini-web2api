"""Stream parser state machine: parses incoming text chunks, separates content and <tool_call> blocks."""

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"


def clean_and_parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """Clean raw tool call text (stripping markdown fences, trailing commas) and parse JSON."""
    text = raw.strip()
    
    # Strip markdown ```json ... ``` if model wrapped it
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug(f"Direct JSON parse failed: {e}. Trying sanitization...")

    # Fallback sanitization: clean trailing commas before } or ]
    sanitized = re.sub(r",\s*([\}\]])", r"\1", text)
    try:
        data = json.loads(sanitized)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Regex extraction of name and arguments if JSON is truncated or slightly malformed
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    args_match = re.search(r'"arguments"\s*:\s*(\{.*\}|"[^"]*")', text, re.DOTALL)
    if name_match:
        name = name_match.group(1)
        args: Any = {}
        if args_match:
            try:
                args = json.loads(args_match.group(1))
            except Exception:
                args = {"raw": args_match.group(1)}
        return {"name": name, "arguments": args}

    return None


class StreamEvent:
    """Represents an event emitted by the stream parser."""

    def __init__(
        self,
        event_type: str,  # 'content', 'tool_call', 'heartbeat', 'done'
        content: str = "",
        tool_call: Optional[Dict[str, Any]] = None,
        finish_reason: Optional[str] = None,
    ):
        self.event_type = event_type
        self.content = content
        self.tool_call = tool_call
        self.finish_reason = finish_reason

    def __repr__(self) -> str:
        return f"StreamEvent({self.event_type}, content={self.content!r}, tool_call={self.tool_call!r}, finish={self.finish_reason})"


class StreamParser:
    """
    Incremental state machine parser for LLM streaming responses.
    Dispatches normal content as it arrives, intercepts <tool_call> tags,
    and produces structured tool calls for the OpenAI completions API.
    """

    def __init__(self):
        self.buffer = ""
        self.in_tool_call = False
        self.tool_call_buffer = ""
        self.parsed_tool_calls: List[Dict[str, Any]] = []
        self.has_emitted_content = False

    def feed(self, chunk: str) -> List[StreamEvent]:
        """Feed an incoming raw chunk from the browser and return events to emit."""
        events: List[StreamEvent] = []
        self.buffer += chunk

        while self.buffer:
            if not self.in_tool_call:
                # Check for start tag
                open_pos = self.buffer.find(OPEN_TAG)
                if open_pos != -1:
                    # Content before <tool_call>
                    content_before = self.buffer[:open_pos]
                    if content_before:
                        events.append(StreamEvent("content", content=content_before))
                        self.has_emitted_content = True
                    self.in_tool_call = True
                    self.tool_call_buffer = ""
                    self.buffer = self.buffer[open_pos + len(OPEN_TAG):]
                else:
                    # Check for partial start tag at the end of buffer (e.g. "<tool_")
                    partial_match = False
                    for i in range(1, len(OPEN_TAG)):
                        if self.buffer.endswith(OPEN_TAG[:i]):
                            content_safe = self.buffer[:-i]
                            if content_safe:
                                events.append(StreamEvent("content", content=content_safe))
                                self.has_emitted_content = True
                            self.buffer = self.buffer[-i:]
                            partial_match = True
                            break
                    if not partial_match:
                        # No partial tag, whole buffer is safe content
                        events.append(StreamEvent("content", content=self.buffer))
                        self.has_emitted_content = True
                        self.buffer = ""
                    break
            else:
                # We are inside <tool_call>
                close_pos = self.buffer.find(CLOSE_TAG)
                if close_pos != -1:
                    self.tool_call_buffer += self.buffer[:close_pos]
                    self.buffer = self.buffer[close_pos + len(CLOSE_TAG):]
                    self.in_tool_call = False

                    # Process the completed tool call block
                    parsed = clean_and_parse_json(self.tool_call_buffer)
                    if parsed:
                        name = parsed.get("name", "unknown")
                        args = parsed.get("arguments", {})
                        args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args)
                        
                        tc = {
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": args_str,
                            },
                        }
                        self.parsed_tool_calls.append(tc)
                        events.append(StreamEvent("tool_call", tool_call=tc))
                    else:
                        logger.warning(f"Failed to parse tool call payload: {self.tool_call_buffer}")
                    self.tool_call_buffer = ""
                else:
                    # Whole chunk belongs to tool_call_buffer (except possible partial close tag)
                    partial_match = False
                    for i in range(1, len(CLOSE_TAG)):
                        if self.buffer.endswith(CLOSE_TAG[:i]):
                            self.tool_call_buffer += self.buffer[:-i]
                            self.buffer = self.buffer[-i:]
                            partial_match = True
                            break
                    if not partial_match:
                        self.tool_call_buffer += self.buffer
                        self.buffer = ""
                    break

        return events

    def finalize(self) -> List[StreamEvent]:
        """Called when upstream generation has finished. Flushes remaining state."""
        events: List[StreamEvent] = []

        # If ended while inside <tool_call>, attempt to parse what we have
        if self.in_tool_call and self.tool_call_buffer:
            self.tool_call_buffer += self.buffer
            self.buffer = ""
            parsed = clean_and_parse_json(self.tool_call_buffer)
            if parsed:
                name = parsed.get("name", "unknown")
                args = parsed.get("arguments", {})
                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args)
                tc = {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": args_str,
                    },
                }
                self.parsed_tool_calls.append(tc)
                events.append(StreamEvent("tool_call", tool_call=tc))
            else:
                # If cannot parse, dump as normal content
                events.append(StreamEvent("content", content=self.tool_call_buffer))

        # If any remaining safe buffer
        elif self.buffer:
            events.append(StreamEvent("content", content=self.buffer))
            self.buffer = ""

        # Determine finish reason
        finish_reason = "tool_calls" if self.parsed_tool_calls else "stop"
        events.append(StreamEvent("done", finish_reason=finish_reason))
        return events
