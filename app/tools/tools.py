from datetime import datetime, timezone

from langchain_core.tools import tool


@tool
def get_current_time() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


TOOLS = [get_current_time]
