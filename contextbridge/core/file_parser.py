"""
Multi-format file parser for ContextBridge.

Supports:
  - ChatGPT export ZIPs (conversations.json)
  - Plain text (.txt) chat transcripts
  - Markdown (.md) files
  - PDF documents (.pdf)
  - JSON files (.json)
  - HTML saved pages (.html)

Each parser returns a unified text representation that the MemoryExtractor
can process.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_file(file_bytes: bytes, filename: str) -> str:
    """Parse a file and return its text content.

    Args:
        file_bytes: Raw file content as bytes.
        filename: Original filename (used to detect format).

    Returns:
        Extracted text suitable for MemoryExtractor.
    """
    ext = Path(filename).suffix.lower()

    if ext == ".zip":
        return _parse_chatgpt_export(file_bytes)
    elif ext == ".json":
        return _parse_json(file_bytes)
    elif ext == ".pdf":
        return _parse_pdf(file_bytes)
    elif ext == ".html" or ext == ".htm":
        return _parse_html(file_bytes)
    elif ext in (".txt", ".md", ".text", ".log"):
        return file_bytes.decode("utf-8", errors="replace")
    else:
        # Best-effort: try as text
        return file_bytes.decode("utf-8", errors="replace")


def parse_multiple_files(files: list[tuple[bytes, str]]) -> str:
    """Parse multiple files and combine their content.

    Args:
        files: List of (file_bytes, filename) tuples.

    Returns:
        Combined text from all files.
    """
    sections = []
    for file_bytes, filename in files:
        try:
            text = parse_file(file_bytes, filename)
            if text.strip():
                sections.append(f"--- File: {filename} ---\n{text}")
        except Exception as e:
            sections.append(f"--- File: {filename} (parse error: {e}) ---")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# ChatGPT Export (ZIP → conversations.json)
# ---------------------------------------------------------------------------


def _parse_chatgpt_export(data: bytes) -> str:
    """Parse a ChatGPT data export ZIP file.

    The ZIP contains conversations.json — an array of conversation objects.
    Each conversation has a 'mapping' dict with message nodes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Look for conversations.json
            names = zf.namelist()
            conv_file = None
            for name in names:
                if "conversations.json" in name.lower():
                    conv_file = name
                    break

            if not conv_file:
                # No conversations.json — try to read all text files
                texts = []
                for name in names:
                    if name.endswith((".txt", ".md", ".json")):
                        texts.append(zf.read(name).decode("utf-8", errors="replace"))
                return "\n\n".join(texts) if texts else "Empty ZIP archive"

            raw = zf.read(conv_file).decode("utf-8")
            conversations = json.loads(raw)

            return _format_chatgpt_conversations(conversations)
    except zipfile.BadZipFile:
        return data.decode("utf-8", errors="replace")


def _format_chatgpt_conversations(conversations: list[dict]) -> str:
    """Convert ChatGPT conversations.json into readable text."""
    output = []

    for conv in conversations:
        title = conv.get("title", "Untitled")
        output.append(f"\n=== Conversation: {title} ===\n")

        mapping = conv.get("mapping", {})
        if not mapping:
            continue

        # Build message tree: find messages in order
        messages = _extract_messages_from_mapping(mapping)

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content.strip():
                continue

            if role == "user":
                output.append(f"User: {content}")
            elif role == "assistant":
                output.append(f"Assistant: {content}")
            elif role == "system":
                output.append(f"System: {content}")

    return "\n\n".join(output)


def _extract_messages_from_mapping(mapping: dict) -> list[dict]:
    """Extract ordered messages from ChatGPT's nested mapping structure."""
    messages = []

    # Find root node (no parent)
    root_id = None
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            root_id = node_id
            break

    if not root_id:
        # Fallback: just iterate all nodes
        for node_id, node in mapping.items():
            msg = node.get("message")
            if msg and msg.get("content"):
                content = msg["content"]
                if isinstance(content, dict):
                    parts = content.get("parts", [])
                    text = "\n".join(str(p) for p in parts if isinstance(p, str))
                elif isinstance(content, str):
                    text = content
                else:
                    text = str(content)

                if text.strip():
                    messages.append(
                        {
                            "role": msg.get("author", {}).get("role", "unknown"),
                            "content": text,
                        }
                    )
        return messages

    # Walk the tree from root following children
    visited = set()
    queue = [root_id]
    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)

        node = mapping.get(node_id, {})
        msg = node.get("message")

        if msg:
            content = msg.get("content")
            if isinstance(content, dict):
                parts = content.get("parts", [])
                text = "\n".join(str(p) for p in parts if isinstance(p, str))
            elif isinstance(content, str):
                text = content
            else:
                text = str(content) if content else ""

            role = msg.get("author", {}).get("role", "unknown")

            if text.strip() and role in ("user", "assistant", "system"):
                messages.append({"role": role, "content": text})

        children = node.get("children", [])
        queue.extend(children)

    return messages


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _parse_json(data: bytes) -> str:
    """Parse a JSON file — could be ChatGPT conversations or generic JSON."""
    text = data.decode("utf-8", errors="replace")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text  # Return as raw text

    # Check if it looks like ChatGPT conversations.json (list of conv objects)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        if "mapping" in parsed[0] or "title" in parsed[0]:
            return _format_chatgpt_conversations(parsed)

    # Check if it's an array of messages [{role, content}]
    if isinstance(parsed, list) and parsed and "role" in parsed[0]:
        lines = []
        for msg in parsed:
            role = msg.get("role", "unknown").capitalize()
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        return "\n\n".join(lines)

    # Generic JSON — just pretty-print
    return json.dumps(parsed, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _parse_pdf(data: bytes) -> str:
    """Extract text from a PDF file."""
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                pages.append(f"[Page {i + 1}]\n{text.strip()}")

        if pages:
            return "\n\n".join(pages)
        else:
            return "[PDF contained no extractable text — may be image-based]"

    except ImportError:
        return "[PDF parsing requires PyPDF2: pip install PyPDF2]"
    except Exception as e:
        return f"[PDF parse error: {e}]"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _parse_html(data: bytes) -> str:
    """Extract text from an HTML file (e.g., saved web chat page)."""
    text = data.decode("utf-8", errors="replace")

    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Replace common block elements with newlines
    text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode HTML entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"  +", " ", text)

    return text.strip()
