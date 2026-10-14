"""Prompt compiler: Injects tool specifications and calling instructions into prompts."""

import json
from typing import Any, Dict, List, Optional


TOOL_CALL_INSTRUCTION = """
# TOOL CALLING PROTOCOL
You have access to the tools listed below. If you need to perform an action (such as executing commands, reading or writing files), you MUST invoke the appropriate tool using the specified XML block format.

## Available Tools:
{tools_doc}

## Calling Rules:
1. When invoking a tool, your response MUST contain a `<tool_call>` block with a valid JSON payload specifying `name` and `arguments`.
Example:
<tool_call>
{{
  "name": "bash",
  "arguments": {{
    "command": "ls -la"
  }}
}}
</tool_call>

2. Ensure the JSON inside `<tool_call>` is strictly valid JSON (double quotes for keys/strings, correct escaping).
3. Do NOT wrap the JSON inside markdown triple backticks (```) within the `<tool_call>` tag.
4. CRITICAL: Immediately after closing `</tool_call>`, STOP GENERATING. NEVER simulate or invent the tool result. The external environment will execute the tool and provide the real output in the next turn.
5. If no tool is required, respond normally without `<tool_call>` tags.
"""


def format_tool_definition(tool: Dict[str, Any]) -> str:
    """Format an individual tool schema into readable documentation for the model."""
    func = tool.get("function", tool)
    name = func.get("name", "unknown")
    desc = func.get("description", "No description provided.")
    params = func.get("parameters", {})
    
    params_str = json.dumps(params, indent=2, ensure_ascii=False)
    return f"### `{name}`\nDescription: {desc}\nParameters JSON Schema:\n```json\n{params_str}\n```\n"


def compile_system_prompt(
    original_system: Optional[str],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Combine system prompt with tool calling instructions, compacting for web UI limits."""
    parts = []

    if tools:
        tools_doc = "\n".join(format_tool_definition(t) for t in tools)
        parts.append(TOOL_CALL_INSTRUCTION.format(tools_doc=tools_doc).strip())

    if original_system and original_system.strip():
        sys_clean = original_system.strip()
        # Web chat interfaces (like Gemini Web) choke on massive 35KB CLI agent prompts.
        # Extract the essential instructions (first 1500 chars).
        if len(sys_clean) > 2000:
            sys_clean = sys_clean[:1500] + "\n...(truncated for web interface efficiency)..."
        parts.append(sys_clean)

    return "\n\n".join(parts)

def format_message_content(content: Any) -> str:
    """Normalize message content (string or array of text/image blocks) to string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    text_parts.append("[Image attached]")
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)
    return str(content) if content is not None else ""


def format_tool_result_message(tool_msg: Dict[str, Any], tool_name_map: Optional[Dict[str, str]] = None) -> str:
    """Format a tool result message into a clean readable prompt for the web interface."""
    tool_call_id = tool_msg.get("tool_call_id", "")
    name = tool_msg.get("name")
    if not name and tool_name_map and tool_call_id in tool_name_map:
        name = tool_name_map[tool_call_id]
    name_str = f" (`{name}`)" if name else ""
    
    content = format_message_content(tool_msg.get("content", ""))
    return f"**[Tool Result{name_str}]**\n```\n{content}\n```"
