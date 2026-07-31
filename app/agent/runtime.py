from functools import lru_cache

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings
from app.db.models import Conversation
from app.db.session import async_session_maker
from app.tools.billing_tools import ALL_TOOLS as BILLING_TOOLS
from app.tools.inventory_tools import ALL_TOOLS as INVENTORY_TOOLS

ALL_TOOLS = [*INVENTORY_TOOLS, *BILLING_TOOLS]
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}

SYSTEM_PROMPT = (
    "You are BigMart's inventory and billing assistant. You have tools to create "
    "products, record received stock, look up stock levels, update a product's GST "
    "slab, and run bills (start_bill, add_item, edit_item, finalize_bill). Never "
    "state a price, "
    "quantity, or bill total from memory or estimation — always call the "
    "appropriate tool and report exactly what it returns. If a tool reports "
    "multiple matching products, ask the user to clarify which one they mean "
    "instead of guessing. Remember the bill_id returned by start_bill and reuse "
    "it for add_item/edit_item/finalize_bill in the rest of this conversation "
    "unless the user clearly starts a new bill. Use edit_item (not add_item) "
    "when the user wants to set a line's quantity to an absolute value, e.g. "
    "'remove X and add N instead' means edit_item(..., new_quantity=N)."
)

MAX_TOOL_ITERATIONS = 8
MAX_HISTORY_MESSAGES = 40


@lru_cache
def get_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        api_key=settings.google_api_key,
    ).bind_tools(ALL_TOOLS)


async def _load_history(thread_id: str) -> list[BaseMessage]:
    async with async_session_maker() as session:
        convo = await session.get(Conversation, thread_id)
        if convo is None:
            return []
        return messages_from_dict(convo.messages[-MAX_HISTORY_MESSAGES:])


async def _save_history(thread_id: str, messages: list[BaseMessage]) -> None:
    serialized = messages_to_dict(messages[-MAX_HISTORY_MESSAGES:])
    async with async_session_maker() as session:
        convo = await session.get(Conversation, thread_id)
        if convo is None:
            session.add(Conversation(thread_id=thread_id, messages=serialized))
        else:
            convo.messages = serialized
        await session.commit()


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


async def _call_tool(name: str, args: dict) -> str:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    try:
        return str(await tool.ainvoke(args))
    except Exception as exc:  # noqa: BLE001 - surface the failure back to the model
        return f"Tool '{name}' raised an error: {exc}"


async def run_agent(message: str, thread_id: str = "default") -> str:
    history = await _load_history(thread_id)
    messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT), *history, HumanMessage(content=message)]

    llm = get_llm()
    for _ in range(MAX_TOOL_ITERATIONS):
        ai_message: AIMessage = await llm.ainvoke(messages)
        messages.append(ai_message)

        tool_calls = ai_message.tool_calls or []
        if not tool_calls:
            break

        for call in tool_calls:
            result = await _call_tool(call["name"], call["args"])
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    else:
        messages.append(AIMessage(content="I couldn't finish that after several tool calls — could you rephrase?"))

    await _save_history(thread_id, messages[1:])  # drop the leading SystemMessage
    return _extract_text(messages[-1].content)
