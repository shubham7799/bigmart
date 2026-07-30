from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from app.config import settings
from app.tools.inventory_tools import ALL_TOOLS

SYSTEM_PROMPT = (
    "You are BigMart's inventory assistant. You have tools to create products, "
    "record received stock, and look up stock levels. Never state a price or "
    "quantity from memory or estimation — always call the appropriate tool and "
    "report exactly what it returns. If a tool reports multiple matching "
    "products, ask the user to clarify which one they mean instead of guessing."
)


@lru_cache
def get_agent():
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        api_key=settings.google_api_key,
    )
    return create_react_agent(llm, ALL_TOOLS, prompt=SYSTEM_PROMPT)


def _extract_text(content: str | list | dict) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text", "") or str(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts) if parts else str(content)
    return str(content)


async def run_agent(message: str) -> str:
    result = await get_agent().ainvoke({"messages": [("user", message)]})
    return _extract_text(result["messages"][-1].content)
