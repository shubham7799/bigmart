from functools import lru_cache

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from app.config import settings
from app.tools.tools import TOOLS


@lru_cache
def get_agent():
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        api_key=settings.google_api_key,
    )
    return create_react_agent(llm, TOOLS)


async def run_agent(message: str) -> str:
    result = await get_agent().ainvoke({"messages": [("user", message)]})
    return result["messages"][-1].content
