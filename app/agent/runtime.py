from dataclasses import dataclass, field
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
from app.tools.document_tools import ALL_TOOLS as DOCUMENT_TOOLS
from app.tools.inventory_tools import ALL_TOOLS as INVENTORY_TOOLS
from app.tools.khata_tools import ALL_TOOLS as KHATA_TOOLS
from app.tools.preference_tools import ALL_TOOLS as PREFERENCE_TOOLS

ALL_TOOLS = [*INVENTORY_TOOLS, *BILLING_TOOLS, *KHATA_TOOLS, *DOCUMENT_TOOLS, *PREFERENCE_TOOLS]
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}


@dataclass
class AgentReply:
    text: str
    files: list[str] = field(default_factory=list)

SYSTEM_PROMPT = (
    "You are BigMart's inventory and billing assistant. You have tools to create "
    "products, record received stock, look up stock levels, update a product's GST "
    "slab, run bills (start_bill, add_item, edit_item, finalize_bill), and manage "
    "customer khata (credit ledger) via khata_add/khata_pay/khata_balance. Never "
    "state a price, "
    "quantity, bill total, or khata balance from memory or estimation — always "
    "call the appropriate tool and report exactly what it returns. If a tool "
    "reports multiple matching products, ask the user to clarify which one they "
    "mean instead of guessing. Remember the bill_id returned by start_bill and "
    "reuse it for add_item/edit_item/finalize_bill in the rest of this "
    "conversation unless the user clearly starts a new bill. Use edit_item (not "
    "add_item) when the user wants to set a line's quantity to an absolute "
    "value, e.g. 'remove X and add N instead' means edit_item(..., "
    "new_quantity=N). If the user wants to finalize a bill 'on credit' or 'on "
    "khata' or says the customer will pay later, call finalize_bill with "
    "on_credit=True instead of using khata_add separately — finalize_bill "
    "already adds the credit entry for you in that case. Use get_invoice to "
    "generate and send a PDF tax invoice for a finalized bill, and "
    "get_sales_analysis to generate and send a sales/stock/GST analysis slide "
    "deck for a date range — both are delivered to the user as a file "
    "automatically, you don't need to do anything extra to send them. "
    "You also have standing shop preferences (set_preference/get_preference/"
    "list_preferences) that persist across restarts and across every chat, not "
    "just this conversation — things like shop_name, gstin, shop_address, and "
    "default_payment_method. Check get_preference proactively instead of "
    "asking the owner to repeat shop details you might already know (e.g. "
    "before or while generating an invoice). If the owner tells you a shop "
    "detail or a standing preference (\"my shop is called X\", \"my GSTIN is "
    "Y\", \"always default to cash\"), call set_preference to remember it for "
    "next time rather than only using it for the current reply."
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


async def _call_tool(name: str, args: dict) -> tuple[str, str | None]:
    """Returns (text_for_model, file_path). Most tools just return a plain
    string, which passes straight through as text_for_model with file_path=None.
    Document-producing tools (get_invoice, get_sales_analysis) instead return a
    {"text": ..., "file_path": ...} dict — that shape is how a tool signals "this
    turn produced a file" back out of the executor. The file_path is pulled out
    here and never becomes part of the ToolMessage the model sees (so the model's
    context never contains a raw filesystem path, and the path never gets
    persisted into the Conversation history — it's a local temp file, meaningless
    after this turn/process). run_agent collects these into AgentReply.files,
    which the webhook layer uses to call send_document."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool: {name}", None
    try:
        result = await tool.ainvoke(args)
    except Exception as exc:  # noqa: BLE001 - surface the failure back to the model
        return f"Tool '{name}' raised an error: {exc}", None

    if isinstance(result, dict) and "file_path" in result:
        return result.get("text", f"Generated file: {result['file_path']}"), result["file_path"]
    return str(result), None


async def run_agent(message: str, thread_id: str = "default") -> AgentReply:
    history = await _load_history(thread_id)
    messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT), *history, HumanMessage(content=message)]

    collected_files: list[str] = []
    llm = get_llm()
    for _ in range(MAX_TOOL_ITERATIONS):
        ai_message: AIMessage = await llm.ainvoke(messages)
        messages.append(ai_message)

        tool_calls = ai_message.tool_calls or []
        if not tool_calls:
            break

        for call in tool_calls:
            text_result, file_path = await _call_tool(call["name"], call["args"])
            messages.append(ToolMessage(content=text_result, tool_call_id=call["id"]))
            if file_path:
                collected_files.append(file_path)
    else:
        messages.append(AIMessage(content="I couldn't finish that after several tool calls — could you rephrase?"))

    await _save_history(thread_id, messages[1:])  # drop the leading SystemMessage
    return AgentReply(text=_extract_text(messages[-1].content), files=collected_files)
