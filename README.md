# BigMart — Kirana Store Telegram Agent

A FastAPI + LangChain (Gemini) backend that runs a kirana (grocery) store's
inventory, billing/GST, customer credit (khata), and document generation
through a single Telegram chat, backed by Postgres.

## Harness choice

**LangChain's `ChatGoogleGenerativeAI.bind_tools()` with a hand-rolled
tool-calling loop — no LangGraph, no `create_tool_calling_agent`/
`AgentExecutor`.**

The project started on LangGraph's `create_react_agent` (Phase 0–1), but was
deliberately moved off it (Phase 3, see [app/agent/runtime.py](app/agent/runtime.py))
in favor of writing the loop directly:

```python
llm = ChatGoogleGenerativeAI(model=...).bind_tools(ALL_TOOLS)
for _ in range(MAX_TOOL_ITERATIONS):
    ai_message = await llm.ainvoke(messages)
    messages.append(ai_message)
    if not ai_message.tool_calls:
        break
    for call in ai_message.tool_calls:
        result = await tool.ainvoke(call["args"])
        messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
```

Reasons, concretely:

- **Agent-first by construction, not by discipline.** There is exactly one
  place a tool gets called — inside this loop, in response to the model's own
  `tool_calls`. There is no per-intent node graph to accidentally route around,
  no separate "billing intent" vs "inventory intent" branch that a future edit
  could silently bypass the model with. Every clarifying question ("which
  Maggi variant?") is the model reading a tool's return value, never a
  keyword check on the user's message (audited explicitly in Phase 7 — see
  below).
- **Full control over what persists and how.** Chat history is stored in our
  own `Conversation` table (see "Cross-session memory" below) rather than a
  framework-owned checkpointer, and document generation needed a custom
  side-channel (a tool returns `{"text", "file_path"}` instead of a plain
  string) to get a file from a tool call back out to the Telegram layer
  without leaking a raw filesystem path into the model's context. Both were
  simpler to build directly against `langchain_core` primitives than to bend
  a framework's agent executor around.
- **One fewer moving part in production.** No checkpointer backend to
  provision (we tried Postgres-backed LangGraph checkpointing in Phase 3
  before removing LangGraph in Phase 4/5 entirely), no framework-version
  coupling for the core request loop — just `ChatGoogleGenerativeAI` and
  plain Python control flow.

## Control loop

One sentence per step, per incoming Telegram message:

1. Telegram POSTs an update to `/webhook` ([app/main.py](app/main.py)); FastAPI hands it to
   a background task and returns `200` immediately, so a slow LLM call can
   never cause Telegram to see a timeout and retry.
2. `process_update` ([app/telegram/webhook.py](app/telegram/webhook.py)) atomically records the
   Telegram `update_id` in `ProcessedUpdate` via `INSERT ... ON CONFLICT DO
   NOTHING`; if it's already been seen (duplicate delivery), processing stops
   right there, before the agent or any tool ever runs.
3. `run_agent` ([app/agent/runtime.py](app/agent/runtime.py)) loads that chat's prior message
   history from `Conversation`, appends the new user message, and calls
   Gemini with every tool bound via `.bind_tools()`.
4. If Gemini's response includes tool calls, each one runs against Postgres
   (inventory/billing/khata/document/preference tools) and its result is fed
   back as a `ToolMessage`; this repeats until Gemini stops calling tools or
   an 8-iteration cap is hit.
5. The updated message history is saved back to `Conversation`, and the
   final reply text plus any generated file paths are returned together as
   an `AgentReply`.
6. `process_update` sends the reply via `send_message`, sends any generated
   PDF/PPTX via `send_document` and deletes the temp file afterward; any
   exception anywhere in this chain is caught, logged server-side, and
   turned into a plain "something went wrong, try again" message to the user
   instead of silence.

## Tool / skill design

Each file owns one concern and only talks to Postgres — no tool file imports
Telegram or the agent runtime:

| File | Owns |
|---|---|
| [app/tools/inventory_tools.py](app/tools/inventory_tools.py) | Products & stock: `add_product`, `receive_stock`, `update_gst`, `get_stock` |
| [app/tools/billing_tools.py](app/tools/billing_tools.py) | Bill lifecycle & GST: `start_bill`, `add_item`, `edit_item`, `finalize_bill` |
| [app/tools/khata_tools.py](app/tools/khata_tools.py) | Customer credit ledger: `khata_add`, `khata_pay`, `khata_balance` |
| [app/tools/document_tools.py](app/tools/document_tools.py) | PDF/PPTX generation handoff: `get_invoice`, `get_sales_analysis` |
| [app/tools/preference_tools.py](app/tools/preference_tools.py) | Standing shop settings: `set_preference`, `get_preference`, `list_preferences` |
| [app/services/gst.py](app/services/gst.py) | Pure GST math (no DB) |
| [app/services/invoice_pdf.py](app/services/invoice_pdf.py) | Pure PDF rendering (no Telegram/agent imports) |
| [app/services/analysis_pptx.py](app/services/analysis_pptx.py) | Pure PPTX rendering + DB aggregation |

## How the hard parts were solved

**Grounding (Phase 1).** Every number the model states — a price, a stock
quantity, a bill total — has to come from a tool's return value, never from
the model's own arithmetic or memory. Enforced two ways: the system prompt in
`app/agent/runtime.py` explicitly says "never state a price/quantity/total
from memory," and structurally, every tool computes and returns the real
number itself (e.g. `get_stock` in `inventory_tools.py` returns
`Product.quantity_on_hand` straight from the row it just queried). Fuzzy name
matches that could be ambiguous (`_find_products` in `inventory_tools.py`)
return the full list of candidates rather than picking one, so the model is
forced to ask rather than guess.

**Oversell guard (Phase 2).** `add_item` and `edit_item` in
`billing_tools.py` compare the requested quantity against
`Product.quantity_on_hand` and reject (no DB write at all) if it would go
negative — this is a hard `if` in the tool, not a prompt instruction the
model could talk its way around. `finalize_bill` re-runs the same check
*after* acquiring row locks (see next section), because stock can move
between when an item was added to a bill and when that bill is finalized.

**Idempotency + concurrency (Phase 3).** Two separate problems, two separate
fixes. Duplicate Telegram deliveries of the same `update_id` are caught by a
single atomic `INSERT ... ON CONFLICT DO NOTHING` in
`_try_mark_processed` (`app/telegram/webhook.py`) — the uniqueness check and
the write happen as one statement, so two near-simultaneous deliveries can't
both pass. Concurrent `finalize_bill` calls (double-tap, or two different
bills racing for the same stock) are handled with `SELECT ... FOR UPDATE` on
the `Bill` row first, then every touched `Product` row in a fixed
ascending-id order (to avoid lock-order deadlocks across different bills),
all inside one transaction — see the long comment in `finalize_bill` for the
full reasoning, including why this specifically requires Postgres.

**Cross-session memory (Phase 0 + Phase 6 — two distinct layers).**
`Conversation` (`app/db/models.py`) holds per-chat-thread message history so
the model remembers e.g. an in-progress `bill_id` across turns within one
Telegram chat — scoped by `thread_id`, capped at the last 40 messages, and
explicitly *not* meant to survive into a different chat. `Preference`
(`app/db/models.py`, `app/tools/preference_tools.py`) is the opposite: a
flat key/value table scoped globally to the single shop, not to any chat or
user, holding things like `shop_name`/`gstin`/`default_payment_method` that
should be set once and used everywhere — including by other tools
(`invoice_pdf.py` reads `shop_name`/`gstin` for the invoice header,
`finalize_bill` reads `default_payment_method` as a fallback). The two were
kept deliberately separate rather than merged into one memory system.

## Known simplifications

Being upfront about what's *not* handled, rather than hiding it:

- **Customer/product identity is exact-or-substring name matching only** — no
  fuzzy matching, no phone-based customer dedup. Two different real customers
  named "Ramesh" collide onto the same khata ledger. (`app/db/models.py`,
  `Customer` docstring.)
- **Preferences and `default_payment_method` are single-shop/global**, not
  per-customer. A per-customer override would need a keyed convention like
  `default_payment_method:<customer_name>` — noted as an extension point in
  `finalize_bill` but not built.
- **No migration tool (Alembic).** Schema changes on an existing table need a
  manual `ALTER TABLE` — `Base.metadata.create_all()` only creates missing
  tables, it never alters existing ones. This bit us for real in Phase 6
  (`Bill.payment_method`) and will again for any future column addition.
- **Shop identity is placeholder text until set.** Before `set_preference` is
  called for `shop_name`/`gstin`/`shop_address`, invoices show a labeled
  `[... not set — say "..." to set it]` placeholder rather than a blank or an
  invented value.
- **Conversation history is capped at 40 messages** (`MAX_HISTORY_MESSAGES`
  in `runtime.py`); a very long-running chat will lose earlier context.
- **`finalize_bill`'s row-locking guarantee is Postgres-specific.** SQLite
  locks the whole database file on write rather than individual rows, so
  `SELECT ... FOR UPDATE` there doesn't give the same fine-grained
  concurrency guarantee (and some SQLite drivers ignore the syntax entirely).
  This app must run against Postgres, not a SQLite file, for the Phase 3
  guarantees to actually hold — see the comment in `finalize_bill`.
- **No auth on `/webhook` beyond the bot token being secret.** Anyone who
  discovers the deployed URL and can forge a Telegram-shaped JSON payload
  could interact with the bot. Acceptable for a single-shop-owner bot behind
  an unguessable URL, but worth knowing.
- **Tool-call loop is capped at 8 iterations** (`MAX_TOOL_ITERATIONS`); an
  unusually long multi-step request could hit this and get an apologetic
  "please rephrase" instead of finishing.

## Setup — running this yourself

### Local

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, DATABASE_URL
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/health   # expect {"status":"ok"}
```

`DATABASE_URL` must point at Postgres (see "Known simplifications" above for
why SQLite isn't safe here), e.g.:
```
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/bigmart
```
Tables are created automatically on startup (`init_db()` in
`app/db/session.py`); the database itself must already exist.

To talk to it locally via real Telegram, expose the server and register the
webhook:
```bash
ngrok http 8000
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<ngrok-id>.ngrok-free.app/webhook"
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"   # confirm no last_error_message
```

### Deploying (Render)

This repo includes [render.yaml](render.yaml) (Render's native blueprint —
defines the web service and a managed Postgres database together) and a
[Procfile](Procfile) as a fallback for a plain start-command-only setup.

1. Push this repo to GitHub, then create a new Blueprint on Render pointing
   at it — `render.yaml` provisions the web service and a free Postgres
   instance together.
2. In the Render dashboard, set the two secret env vars `render.yaml`
   deliberately leaves unset (`sync: false`): `TELEGRAM_BOT_TOKEN` and
   `GOOGLE_API_KEY`. `DATABASE_URL` is wired automatically from the
   provisioned database.
3. Render's managed Postgres hands out a plain `postgresql://` connection
   string; `app/config.py` normalizes this to `postgresql+asyncpg://`
   automatically, so no manual edit is needed.
4. Once deployed, register the **real** deployed URL as the webhook — no
   more ngrok:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-app>.onrender.com/webhook"
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
5. Message the bot on Telegram.

No secrets are committed: `.env` is gitignored, only `.env.example` (with
placeholder values) is tracked.

---

## Phase history

Brief log of what each phase added, for context on how the system evolved.

- **Phase 0 — Skeleton.** FastAPI app, `/webhook` → agent → Telegram reply
  loop, stub tool, `.env` config.
- **Phase 1 — Inventory.** `Product`/`StockTxn` models, `add_product`,
  `receive_stock`, `get_stock`, `update_gst`. Fuzzy-match ambiguity returns a
  candidate list instead of guessing.
- **Phase 2 — Billing + GST.** `Bill`/`BillItem` models, `app/services/gst.py`
  (CGST/SGST split, per-line nearest-rupee rounding), `start_bill`/
  `add_item`/`edit_item`/`finalize_bill` with the oversell guard enforced in
  code.
- **Phase 3 — Idempotency + concurrency.** `ProcessedUpdate` + atomic dedup
  insert in the webhook layer; `SELECT ... FOR UPDATE` row locking in
  `finalize_bill` for both the `Bill` and every touched `Product`, in a
  fixed lock order to avoid deadlocks.
- **Phase 4 — Khata (credit ledger).** `Customer`/`KhataEntry` models,
  `khata_add`/`khata_pay`/`khata_balance`, and `finalize_bill(on_credit=True)`
  reusing the same locked transaction so a duplicate finalize can't
  double-credit.
- **Phase 4.5 — Drop LangGraph.** Replaced `create_react_agent` +
  `AsyncPostgresSaver` with the hand-rolled tool-calling loop and a plain
  `Conversation` table for history, removing the LangGraph dependency
  entirely (see "Harness choice" above).
- **Phase 5 — Documents.** `app/services/invoice_pdf.py` (reportlab) and
  `app/services/analysis_pptx.py` (python-pptx + matplotlib), delivered via
  `send_document`; the `{"text", "file_path"}` tool-return convention that
  threads a generated file back out of the agent loop without it entering
  the model's own context.
- **Phase 6 — Preferences.** `Preference` key/value table, global (not
  per-chat) shop settings, wired into invoice generation and
  `finalize_bill`'s payment-method fallback.
- **Phase 7 — Polish & deployment.** Audited for any hardcoded intent
  routing (none found — see "Tool / skill design" above), tightened tool
  docstrings (especially `add_item` vs `edit_item`), added a user-facing
  fallback message on webhook errors (previously failures were logged but
  the user got silence), Render deployment config, and this README rewrite.
