from langchain_core.tools import tool

from app.services.analysis_pptx import generate_analysis_pptx
from app.services.invoice_pdf import generate_invoice_pdf

# These tools deliberately don't call Telegram themselves — they just generate a
# file and hand back its path. Returning {"text": ..., "file_path": ...} instead
# of a plain string is a convention app/agent/runtime.py's tool-call loop
# specifically recognizes: it pulls file_path out into a side list threaded back
# to the webhook layer (which sends the file via send_document), while "text" is
# what actually goes into the model's own view of the conversation. See the
# comment on _call_tool in app/agent/runtime.py for the full mechanism.


@tool
async def get_invoice(bill_id: int) -> dict:
    """Generate a PDF tax invoice for a finalized bill and send it to the user.
    Only works on finalized bills — returns an error message (no file sent) for
    a draft bill or an unknown bill_id."""
    try:
        path = await generate_invoice_pdf(bill_id)
    except ValueError as exc:
        return {"text": str(exc)}
    return {"text": f"Generated the invoice PDF for bill {bill_id}.", "file_path": path}


@tool
async def get_sales_analysis(date_from: str, date_to: str) -> dict:
    """Generate a sales/stock/GST analysis slide deck (PPTX) covering
    [date_from, date_to] inclusive (both "YYYY-MM-DD") and send it to the user.
    Returns an error message (no file sent) for a malformed or inverted date
    range (date_from after date_to)."""
    try:
        path = await generate_analysis_pptx(date_from, date_to)
    except ValueError as exc:
        return {"text": str(exc)}
    return {
        "text": f"Generated the sales analysis deck for {date_from} to {date_to}.",
        "file_path": path,
    }


ALL_TOOLS = [get_invoice, get_sales_analysis]
