import os
from dotenv import load_dotenv
import asyncio
import imaplib
import smtplib
import email
import json
from typing import Any, Dict, List, Optional, Literal, TypedDict
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from openai import AsyncOpenAI
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
import email.utils

load_dotenv("../.env")


GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
EMAIL_USER = os.getenv("EMAIL_PASS")
EMAIL_PASS = os.getenv("PASSWORD_16_digit")

client = AsyncOpenAI(
    api_key=GITHUB_TOKEN, base_url="https://models.inference.ai.azure.com"
)


class EmailFilter(BaseModel):
    mode: Literal["ALL", "UNSEEN", "SEEN"] = Field(default="ALL")
    sender: Optional[str] = Field(default=None)
    subject_keyword: Optional[str] = Field(default=None)
    since_date: Optional[str] = Field(default=None)
    limit: int = Field(default=5, ge=1, le=50)

    @field_validator("since_date")
    @classmethod
    def validate_imap_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%d-%b-%Y")
            return v
        except ValueError:
            raise ValueError("Date format must be DD-MMM-YYYY (e.g., 01-Jan-2026)")


class AgentState(TypedDict):
    user_query: str
    messages: List[Dict[str, str]]
    search_results: List[Dict[str, Any]]
    current_idx: Optional[int]  # <-- ADD THIS: Tracks selected email index
    current_email: Dict[str, Any]  # Keeps structured keys: 'from', 'subject', 'body'
    draft_reply: str
    approved: bool
    email_filters: Optional[EmailFilter]


def build_imap_criterion(filters: EmailFilter) -> str:
    parts = [filters.mode]
    if filters.sender:
        parts.append(f'FROM "{filters.sender}"')
    if filters.subject_keyword:
        parts.append(f'SUBJECT "{filters.subject_keyword}"')
    if filters.since_date:
        parts.append(f'SINCE "{filters.since_date}"')

    if len(parts) == 1:
        return parts[0]
    return f"({' '.join(parts)})"


async def entrypoint_router(
    state: AgentState,
) -> Literal["fetch_emails", "generate_draft"]:
    # CRITICAL FIX: If a draft already exists, this IS a revision pass!
    # Bypassing the LLM keeps us locked into the revision loop.
    if state.get("draft_reply"):
        print(
            "\n🔄 [ROUTER] Existing draft detected in state memory. Routing straight to generate_draft for revision."
        )
        return "generate_draft"

    print(
        f"\n🧠 [ROUTER] Initial run detected. Classifying query intent: '{state.get('user_query')}'"
    )
    classification_prompt = f'Analyze request: \'{state.get("user_query")}\'. Return JSON matching format: {{"intent": "fetch"}} or {{"intent": "compose"}}.'

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": classification_prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content.strip())
        intent = result.get("intent", "compose")
        target = "fetch_emails" if intent == "fetch" else "generate_draft"
        print(f"🔀 [ROUTER] Direction selected -> {target.upper()}")
        return target
    except Exception as e:
        print(f"⚠️ [ROUTER] Classification failed: {e}. Defaulting to compose.")
        return "generate_draft"


async def generate_draft_node(state: AgentState) -> Dict[str, Any]:
    print("\n📝 [NODE: generate_draft] Analyzing layout paths...")
    user_query = state.get("user_query")
    existing_draft = state.get("draft_reply")
    search_results = state.get("search_results", [])
    current_idx = state.get("current_idx")
    current_email = state.get("current_email") or {}

    if existing_draft:
        print("🔄 [PATH: REVISION] Modifying active draft based on human feedback.")
        print(f"💬 Human Feedback Received: '{user_query}'")

        draft_prompt = f"""
        You are an AI assistant revising an email draft based on direct human feedback instructions.
        
        Original Recipient Context: {current_email.get("from_name", "User")} <{current_email.get("from", "unknown@example.com")}>
        Original Subject: {current_email.get("subject", "No Subject")}
        
        The current draft text version you built previously is:
        \"\"\"
        {existing_draft}
        \"\"\"
        
        The human supervisor wants you to modify that draft text with these exact change requests:
        🚨 CRITIQUE / REVISION INSTRUCTIONS: "{user_query}"
        
        Task: Apply the critique instructions directly to rewrite the draft version.
        Output ONLY the clean, final, updated email message body text. Do not include markdown headers, subject prefixes, or meta commentaries.
        """

    elif current_idx is not None and 0 <= current_idx < len(search_results):
        chosen_email = search_results[current_idx]
        current_email = {
            "from": chosen_email.get("from"),
            "from_name": chosen_email.get("from_name", "User"),
            "subject": chosen_email.get("subject"),
            "body": chosen_email.get("body"),
        }
        print(
            f"📥 [PATH: NEW REPLY] Context locked -> Index [{current_idx}]: {current_email['from']}"
        )

        draft_prompt = f"""
        Write a professional email reply to {current_email["from_name"]} <{current_email["from"]}>.
        Their incoming subject line: '{current_email["subject"]}'
        Their incoming email body context:
        '{current_email["body"]}'
        
        Your response goal instructions: "{user_query}"
        
        Output ONLY the raw email response body text. Do not add subject lines or structural commentary.
        """
    else:
        print("✉️ [PATH: INDEPENDENT COMPOSE] Parsing structural metadata goals...")
        intent_prompt = f"Analyze request: '{user_query}'. Return JSON: {{'recipient_email': '...', 'subject': '...', 'core_content_instructions': '...'}}"
        intent_res = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": intent_prompt}],
            response_format={"type": "json_object"},
        )
        intent = json.loads(intent_res.choices[0].message.content.strip())

        current_email = {
            "from": intent.get("recipient_email", "unknown@example.com"),
            "subject": intent.get("subject", "Automated System Update Notification"),
            "body": "",
        }

        draft_prompt = f"""
        Compose an independent outbound email message from scratch.
        To: {current_email["from"]}
        Subject: {current_email["subject"]}
        Message goals: {intent.get("core_content_instructions")}
        
        Output ONLY the clean raw email body text.
        """

    # Send selected prompt configuration downstream to LLM
    draft_res = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": draft_prompt}],
        temperature=0.6,
    )

    generated_text = draft_res.choices[0].message.content.strip()
    print(
        "📋 [NODE: generate_draft] Generation pass finalized. Sending to verification gate."
    )

    return {
        "draft_reply": generated_text,
        "current_email": current_email,
        "current_idx": current_idx,
    }


async def fetch_emails_node(state: AgentState) -> Dict[str, Any]:
    print("\n📥 [NODE: fetch_emails] Initiating connection matrix...")
    filters = state.get("email_filters") or EmailFilter()

    def blocking_imap_call():
        print(f"🔌 [IMAP THREAD] Connecting to imap.gmail.com for user: {EMAIL_USER}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")
        criterion = build_imap_criterion(filters)
        print(f"🔍 [IMAP THREAD] Using criterion query: {criterion}")
        status, data = mail.search(None, criterion)

        if not data or not data[0]:
            print("🚫 [IMAP THREAD] No tracking matches found inside inbox.")
            mail.logout()
            return []

        mail_ids = data[0].split()
        latest_ids = mail_ids[-filters.limit :][::-1]
        print(f"📝 [IMAP THREAD] Parsing up to {len(latest_ids)} target messages...")

        fetched_emails = []
        for num in latest_ids:
            status, msg_data = mail.fetch(num, "(BODY.PEEK[])")

            if msg_data and msg_data[0]:
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode(errors="ignore")
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors="ignore")

                raw_from = str(msg.get("From", "Unknown"))

            # parseaddr splits 'Name <email@domain.com>' into ('Name', 'email@domain.com')
            display_name, clean_email_address = email.utils.parseaddr(raw_from)

            fetched_emails.append(
                {
                    "id": num.decode("utf-8", errors="ignore"),
                    "from_raw": raw_from,
                    "from_name": display_name or "Unknown Sender",
                    "from": clean_email_address,
                    "subject": str(msg.get("Subject", "(No Subject)")),
                    "date": str(msg.get("Date", "")),
                    "body": body,
                }
            )

        mail.logout()
        return fetched_emails

    fetched_results = await asyncio.to_thread(blocking_imap_call)

    print(
        fetched_results[0]
    )  # this print for verification either all are retrieving exactly or not.

    print(
        f"✅ [NODE: fetch_emails] Hydrated {len(fetched_results)} entries into 'search_results'. Stalling for item index selection."
    )
    print(f"Results: {fetched_results}")
    return {"search_results": fetched_results}


async def human_review_node(state: AgentState) -> Dict[str, Any]:
    print(
        "\n🚧 [NODE: human_review] Halting graph. Triggering human intervention interrupt layer..."
    )

    # 1. Throw down the blocking interrupt payload
    review_package = {
        "prompt": "Please review this generated reply message.",
        "recipient": state.get("current_email", {}).get("from", "Unknown"),
        "draft_to_verify": state.get("draft_reply", ""),
    }

    # Execution freezes completely here until /agent/review calls Command(resume=...)
    human_input = interrupt(review_package)

    print(
        f"🔓 [NODE: human_review] Graph Awoken! Received inbound payload: {human_input}"
    )

    # 2. Extract and validate incoming JSON data formats from FastAPI safely
    if isinstance(human_input, dict):
        decision = human_input.get("decision", "revise")
        feedback = human_input.get("feedback", "")
    else:
        decision = "revise"
        feedback = str(human_input)

    print(
        f"📊 [NODE: human_review] Processed Choice -> Decision: '{decision.upper()}', Feedback Length: {len(feedback or '')}"
    )

    if decision == "approve":
        return {"approved": True}
    else:
        # Route back to drafting loop by replacing query context with human revision notes
        return {
            "approved": False,
            "user_query": feedback
            if feedback
            else "Please adjust and rewrite the email text.",
        }


# --- EDGE ROUTER ---
def review_edge_router(state: AgentState) -> Literal["generate_draft", "transmit_smtp"]:
    print(
        f"\n🔮 [EDGE ROUTER] Evaluating node transition criteria. Approved flag is: {state.get('approved')}"
    )
    if state.get("approved") is True:
        return "transmit_smtp"
    return "generate_draft"


async def transmit_smtp_node(state: AgentState) -> Dict[str, Any]:
    print(
        "\n🚀 [NODE: transmit_smtp] Final authorization verified. Preparing network transmission..."
    )
    current_email = state.get("current_email", {})
    draft_reply = state.get("draft_reply", "")
    recipient = current_email.get("from", "").strip()
    subject = f"Re: {current_email.get('subject', 'Automated Response')}"
    print(f"📧 [SMTP] Target destination locked: <{recipient}>")

    def send_smtp():
        # Construction of clean, standard MIME layout structures
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = recipient
        msg.set_content(draft_reply)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print("📨 [SMTP THREAD] Server successfully transmitted payload packets.")

    try:
        await asyncio.to_thread(send_smtp)
        print("🎉 [NODE: transmit_smtp] Message delivered smoothly.")
    except Exception as e:
        print(f"❌ [SMTP ERROR] Transmission failed: {e}")
    return {}


workflow = StateGraph(AgentState)
# Hydrate Node Map definitions
workflow.add_node("fetch_emails", fetch_emails_node)
workflow.add_node("generate_draft", generate_draft_node)
workflow.add_node("human_review", human_review_node)
workflow.add_node("transmit_smtp", transmit_smtp_node)
# Map edge wiring and routers
workflow.set_conditional_entry_point(
    entrypoint_router,
    {"fetch_emails": "fetch_emails", "generate_draft": "generate_draft"},
)

# Standard edge mappings
workflow.add_edge(
    "fetch_emails", END
)  # Halts here so FastAPI can select item index explicitly
workflow.add_edge("generate_draft", "human_review")

# Dynamic evaluation path routing out of the verification checkpoint gate
workflow.add_conditional_edges(
    "human_review",
    review_edge_router,
    {"generate_draft": "generate_draft", "transmit_smtp": "transmit_smtp"},
)

workflow.add_edge("transmit_smtp", END)

memory_checkpointer = MemorySaver()

# NOTE: Removed the compile-time 'interrupt_after' keyword parameter to prevent
# double-pausing on node exit boundaries as discovered in your testing!
agent_app = workflow.compile(checkpointer=memory_checkpointer)
print(
    "⚙️ [SYSTEM LOG] LangGraph Email Workflow compiled successfully without boundary limits."
)
