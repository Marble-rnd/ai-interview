"""
Tools that an LLM agent can use to research a case.

These are provided as a starting point. You may modify, extend, or replace them.
"""

import json
from pathlib import Path

from .data_loader import list_documents, load_timeline


def search_timeline(case_dir: str, query: str, limit: int = 20) -> list[dict]:
    """Search timeline events by keyword match on content, activity_type, and activity_sub_type.

    Returns matching events (up to `limit`), each with: activity_created_at,
    activity_type, activity_sub_type, direction, and content (truncated to 500 chars).
    """
    events = load_timeline(case_dir)
    query_lower = query.lower()
    matches = []
    for event in events:
        searchable = " ".join(
            [
                event.get("content", ""),
                event.get("activity_type", ""),
                event.get("activity_sub_type", ""),
            ]
        ).lower()
        if query_lower in searchable:
            matches.append(
                {
                    "activity_created_at": event.get("activity_created_at", ""),
                    "activity_type": event.get("activity_type", ""),
                    "activity_sub_type": event.get("activity_sub_type", ""),
                    "direction": event.get("direction", ""),
                    "content": event.get("content", "")[:500],
                }
            )
            if len(matches) >= limit:
                break
    return matches


def get_timeline_summary(case_dir: str) -> dict:
    """Return a high-level summary of the case timeline: event counts by type,
    date range, and total event count."""
    events = load_timeline(case_dir)
    if not events:
        return {"total_events": 0}

    type_counts: dict[str, int] = {}
    for event in events:
        atype = event.get("activity_type", "Unknown")
        type_counts[atype] = type_counts.get(atype, 0) + 1

    dates = [e.get("activity_created_at", "") for e in events if e.get("activity_created_at")]
    return {
        "total_events": len(events),
        "date_range": {"earliest": min(dates), "latest": max(dates)} if dates else {},
        "events_by_type": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
    }


def get_recent_events(case_dir: str, n: int = 10) -> list[dict]:
    """Return the N most recent timeline events."""
    events = load_timeline(case_dir)
    recent = events[-n:]
    return [
        {
            "activity_created_at": e.get("activity_created_at", ""),
            "activity_type": e.get("activity_type", ""),
            "activity_sub_type": e.get("activity_sub_type", ""),
            "direction": e.get("direction", ""),
            "content": e.get("content", "")[:500],
        }
        for e in recent
    ]


def read_document(case_dir: str, filename: str) -> str:
    """Read a document file and return its path. For text extraction from
    PDFs/images, you'll need to implement or integrate an extraction method.

    Returns the full file path so you can process it with your chosen extraction tool.
    """
    doc_path = Path(case_dir) / "documents" / filename
    if not doc_path.exists():
        available = list_documents(case_dir)
        return f"Document '{filename}' not found. Available: {available}"
    return str(doc_path.resolve())


def get_event_details(case_dir: str, event_id: str) -> dict | None:
    """Look up a specific timeline event by its ID. Returns full event data
    including extra_details (parsed as JSON)."""
    events = load_timeline(case_dir)
    for event in events:
        if event.get("id") == event_id:
            result = dict(event)
            extra = result.get("extra_details", "")
            if extra:
                try:
                    result["extra_details"] = json.loads(extra)
                except json.JSONDecodeError:
                    pass
            return result
    return None


# Tool definitions formatted for OpenAI function calling.
# Adapt these if you use a different LLM provider.
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_timeline",
            "description": "Search case timeline events by keyword. Returns matching events with dates, types, and content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword to match against event content, activity type, and sub-type",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 20)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_timeline_summary",
            "description": "Get a high-level summary of the case: total events, date range, and event counts by type.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_events",
            "description": "Get the N most recent timeline events for the case.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of recent events to return (default: 10)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List all document files available for this case.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Get the file path for a case document. You will need to extract text from it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the document file to read",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_details",
            "description": "Get full details for a specific timeline event by its ID, including parsed extra_details metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The UUID of the timeline event",
                    },
                },
                "required": ["event_id"],
            },
        },
    },
]
