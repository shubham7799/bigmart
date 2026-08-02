from langchain_core.tools import tool
from sqlalchemy import select

from app.db.models import Preference
from app.db.session import async_session_maker


async def _find_preference(session, key: str) -> Preference | None:
    stmt = select(Preference).where(Preference.key == key)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_preference_value(session, key: str) -> str | None:
    """Core lookup, usable from within another tool's *existing* session (e.g.
    invoice_pdf.py, billing_tools.finalize_bill) without opening a second DB
    connection mid-transaction. Returns None if unset — callers decide their own
    fallback; this never invents a default."""
    pref = await _find_preference(session, key)
    return pref.value if pref else None


@tool
async def set_preference(key: str, value: str) -> str:
    """Set (or update) a standing shop preference, e.g. key="shop_name",
    value="Sharma General Store". These persist across restarts and across
    every chat — use for things the owner shouldn't have to repeat every
    conversation (shop name, GSTIN, default payment method, preferred brand per
    category, etc)."""
    async with async_session_maker() as session:
        pref = await _find_preference(session, key)
        if pref is None:
            pref = Preference(key=key, value=value)
            session.add(pref)
        else:
            pref.value = value
        await session.commit()
        return f"Set preference '{key}' = '{value}'."


@tool
async def get_preference(key: str) -> str:
    """Look up a standing shop preference by key. Reports clearly if it isn't
    set rather than inventing a default."""
    async with async_session_maker() as session:
        value = await get_preference_value(session, key)
        if value is None:
            return f"No preference set for '{key}'."
        return f"'{key}' = '{value}'."


@tool
async def list_preferences() -> str:
    """List every standing shop preference currently set. Use this to answer
    'what do you know about my shop?' or similar."""
    async with async_session_maker() as session:
        stmt = select(Preference).order_by(Preference.key)
        prefs = (await session.execute(stmt)).scalars().all()
        if not prefs:
            return "No preferences set yet."
        return "\n".join(f"- {p.key} = {p.value}" for p in prefs)


ALL_TOOLS = [set_preference, get_preference, list_preferences]
