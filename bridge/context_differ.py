"""Context differ: Tracks conversation sessions and extracts incremental messages."""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from bridge.prompt_compiler import (
    compile_system_prompt,
    format_message_content,
    format_tool_result_message,
)

logger = logging.getLogger(__name__)


def compute_message_hash(msg: Dict[str, Any]) -> str:
    """Compute a stable hash for a message dict to identify matching prefixes."""
    role = msg.get("role", "")
    content = format_message_content(msg.get("content", ""))
    tool_calls = msg.get("tool_calls", [])
    tool_call_id = msg.get("tool_call_id", "")
    
    rep = f"{role}:{content}:{tool_call_id}:{json.dumps(tool_calls, sort_keys=True)}"
    return hashlib.sha256(rep.encode("utf-8")).hexdigest()[:16]


class SessionState:
    """Maintains state for a single conversation session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.synced_hashes: List[str] = []
        self.tool_call_id_to_name: Dict[str, str] = {}
        self.last_activity: float = 0.0

    def record_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        """Record mappings from tool_call_id to function name."""
        for tc in tool_calls:
            cid = tc.get("id")
            fname = tc.get("function", {}).get("name")
            if cid and fname:
                self.tool_call_id_to_name[cid] = fname


class ContextDiffer:
    """Calculates the delta between stateless API history and stateful web chat."""

    def __init__(self):
        self.sessions: Dict[str, SessionState] = {}
        self.active_session_id: Optional[str] = None
    def get_or_create_session(self, session_id: str) -> SessionState:
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState(session_id)
        return self.sessions[session_id]

    def reset_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            del self.sessions[session_id]

    def diff_and_compile(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, bool]:
        """
        Calculates what needs to be sent to the stateful web interface.
        
        Returns:
            (payload_to_send, need_reset_web_session)
        """
        session = self.get_or_create_session(session_id)
        current_hashes = [compute_message_hash(m) for m in messages]

        # Extract system prompt if present
        system_prompts = [m for m in messages if m.get("role") == "system"]
        raw_system = "\n\n".join(format_message_content(m.get("content", "")) for m in system_prompts)
        compiled_system = compile_system_prompt(raw_system, tools)

        # Check how many messages match the existing synced history prefix
        common_len = 0
        for h_curr, h_prev in zip(current_hashes, session.synced_hashes):
            if h_curr == h_prev:
                common_len += 1
            else:
                break

        is_new_session = (len(session.synced_hashes) == 0)
        history_broken = (common_len < len(session.synced_hashes))
        session_switched = (self.active_session_id is not None and self.active_session_id != session_id)
        self.active_session_id = session_id

        # Also register any tool_calls in assistant messages into map
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                session.record_tool_calls(msg.get("tool_calls"))

        need_reset = is_new_session or history_broken or session_switched

        if need_reset:
            logger.info(
                f"[Session {session_id}] New conversation/reset needed (is_new={is_new_session}, broken={history_broken}, switched={session_switched})"
            )
            compiled_text = self._build_initial_prompt(messages, compiled_system, session)
            session.synced_hashes = list(current_hashes)
            return compiled_text, True

        # Continuation: only send the new messages (from common_len to end)
        new_messages = messages[common_len:]
        if not new_messages:
            logger.warning(f"[Session {session_id}] No new messages detected in request")
            # If nothing new, re-send the last user message or return empty
            return "", False

        delta_text = self._build_delta_prompt(new_messages, session)
        session.synced_hashes = list(current_hashes)
        return delta_text, False

    def _build_initial_prompt(
        self,
        messages: List[Dict[str, Any]],
        compiled_system: str,
        session: SessionState,
    ) -> str:
        """Construct the prompt for turn 1 (or after reset)."""
        parts = []
        if compiled_system:
            parts.append(f"=== System Instructions ===\n{compiled_system}\n===========================")

        # Add conversation messages
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue  # already in compiled_system
            elif role == "user":
                parts.append(format_message_content(msg.get("content", "")))
            elif role == "assistant":
                content = format_message_content(msg.get("content", ""))
                if content:
                    parts.append(f"[Assistant]: {content}")
            elif role == "tool":
                parts.append(format_tool_result_message(msg, session.tool_call_id_to_name))

        return "\n\n".join(parts)

    def _build_delta_prompt(
        self,
        new_messages: List[Dict[str, Any]],
        session: SessionState,
    ) -> str:
        """Construct the delta prompt for subsequent turns (e.g. tool results)."""
        parts = []
        for msg in new_messages:
            role = msg.get("role")
            if role == "tool":
                parts.append(format_tool_result_message(msg, session.tool_call_id_to_name))
            elif role == "user":
                parts.append(format_message_content(msg.get("content", "")))
            elif role == "assistant":
                # CLI agent sent assistant message back; web already has assistant output, but if needed:
                content = format_message_content(msg.get("content", ""))
                if content:
                    parts.append(f"[Assistant Note]: {content}")

        return "\n\n".join(parts)
