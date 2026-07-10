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
import re

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


class RouterPayload(BaseModel):
    intent: Literal["fetch", "compose"] = Field(
        description="Choose 'fetch' if the user wants to check, read, search, list, or look through their email inbox. Choose 'compose' if they want to directly write, reply, or draft a brand new message."
    )
    # We reuse your exact, existing EmailFilter here as a nested field!
    email_filters: Optional[EmailFilter] = Field(
        default=None,
        description="Provide extraction details ONLY if the intent is 'fetch'. Otherwise, leave it as null.",
    )


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
    # 1. Immediate Bypass for Revisions (Production Safety Lock)
    if state.get("draft_reply"):
        return "generate_draft"

    user_query = state.get("user_query", "").strip()
    user_query_lower = user_query.lower()

    # 2. DETERMINISTIC LAYER: Regular Expression Keywords (Instant & Free)
    # Checks for action verbs at the beginning or within the prompt context
    fetch_patterns = r"\b(fetch|read|check|search|find|get|show|list|look)\b"
    if re.search(fetch_patterns, user_query_lower):
        print("🔀 [ROUTER] Deterministic pattern matched -> FETCH_EMAILS")
        return "fetch_emails"

    # Direct writing triggers
    compose_patterns = r"\b(write|compose|send|draft|reply|email|respond)\b"
    if re.search(compose_patterns, user_query_lower):
        print("🔀 [ROUTER] Deterministic pattern matched -> GENERATE_DRAFT")
        return "generate_draft"

    # 3. LLM FALLBACK LAYER: Only called if the query is ambiguous
    # Example query: "Can you see what John wanted yesterday?" (No explicit fetch/write verbs)
    print(
        f"🧠 [ROUTER] Query ambiguous ('{user_query}'). Invoking LLM fallback classifier..."
    )

    class RoutingDecision(BaseModel):
        intent: Literal["fetch", "compose"]

    try:
        response = await client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Classify the user intent. Choose 'fetch' if they want to look at/search inbox history. Choose 'compose' if they want to create a new message.",
                },
                {"role": "user", "content": f"Query: '{user_query}'"},
            ],
            temperature=0.0,  # Forces maximum consistency
            response_format=RoutingDecision,
        )
        decision = response.choices[0].message.parsed

        if decision and decision.intent == "fetch":
            print("🔀 [ROUTER] LLM Fallback -> FETCH_EMAILS")
            return "fetch_emails"

    except Exception as e:
        print(f"⚠️ [ROUTER] Fallback LLM tracking failed: {e}")

    # 4. Safe Default
    print("🔀 [ROUTER] Default fallback path executed -> GENERATE_DRAFT")
    return "generate_draft"


def resolve_email_from_name(target_name: str) -> Optional[str]:
    """
    Connects to IMAP, fetches the most recent emails, and returns the
    address of the first sender that matches the requested name.
    """
    print(f"🔍 [RESOLVER] Searching for name: '{target_name}'...")

    try:
        # 1. Connect and fetch normally
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        # Pull the last 20 emails to scan for our contact
        status, data = mail.search(None, "ALL")
        if not data or not data[0]:
            mail.logout()
            return None

        mail_ids = data[0].split()
        latest_ids = mail_ids[-20:][::-1]  # Check the 20 newest emails

        # 2. Loop through headers and clean them up
        for num in latest_ids:
            status, msg_data = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")

            if msg_data and msg_data[0]:
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                raw_from = str(msg.get("From", ""))

                # Clean up the name and the email address cleanly
                display_name, clean_email_address = email.utils.parseaddr(raw_from)

                # 3. Check for a match using standard Python text checking
                # Example: If "Parikh" is inside "Parikh Jain", it's a match!
                if (
                    target_name.lower() in display_name.lower()
                    or target_name.lower() in raw_from.lower()
                ):
                    print(
                        f"🎯 [RESOLVER] Found Match! '{target_name}' is {clean_email_address}"
                    )
                    mail.logout()
                    return clean_email_address

        mail.logout()
        print(f"❌ [RESOLVER] Could not find any recent emails from '{target_name}'.")

    except Exception as e:
        print(f"❌ [RESOLVER] Error looking up name: {e}")

    return None


async def generate_draft_node(state: AgentState) -> Dict[str, Any]:
    print("\n📝 [NODE: generate_draft] Analyzing layout paths...")
    user_query = state.get("user_query")
    existing_draft = state.get("draft_reply")
    search_results = state.get("search_results", [])
    current_idx = state.get("current_idx")
    current_email = state.get("current_email") or {}

    recipient_email = str(current_email.get("from", "")).strip()
    recipient_name = str(current_email.get("from_name", "")).strip()

    # Check if a clean address format pattern is missing (e.g. name@domain.com)
    is_valid_email = bool(re.search(r"[\w\.-]+@[\w\.-]+\.\w+", recipient_email))

    # IF address is empty/invalid, BUT a valid name string is sitting in memory
    if not is_valid_email and recipient_name and recipient_name.lower() != "user":
        print(
            f"👤 [GUARDRAIL] Missing address detected for name '{recipient_name}'. Launching background IMAP resolver..."
        )

        # Offload your synchronous IMAP search function safely to a background worker thread
        resolved_address = await asyncio.to_thread(
            resolve_email_from_name, recipient_name
        )

        if resolved_address:
            current_email["from"] = resolved_address
        else:
            print(
                "❌ [GUARDRAIL] Name lookup returned None. Applying fallback address boundaries."
            )
            current_email["from"] = "unknown@example.com"

    # If the email is completely empty and couldn't be resolved, apply a baseline fallback string
    if not current_email.get("from"):
        current_email["from"] = "unknown@example.com"
    # =====================================================================

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

        # 1. Clear instructions given inside the prompt
        intent_prompt = f"Analyze request: '{user_query}' if recipient_email didnt found use NOT_FOUND in its place,recipient_name didnt found use NOT_FOUND in its place . Return JSON: {{'recipient_email': '...', 'recipient_name':'...','subject': '...', 'core_content_instructions': '...'}}"

        intent_res = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": intent_prompt}],
            response_format={"type": "json_object"},
        )

        # Access the message content using the modern choices[0].message syntax
        intent = json.loads(intent_res.choices[0].message.content.strip())

        # Initialize your safe default address
        email_address = "unknown@example.com"

        # 2. Extract values directly from our parsed intent JSON object
        extracted_email = intent.get("recipient_email", "NOT_FOUND")
        extracted_name = intent.get("recipient_name", "NOT_FOUND")

        # 3. FIX: Handle the lookups using the correct dictionary keys
        if extracted_email == "NOT_FOUND":
            if extracted_name == "NOT_FOUND":
                pass
            else:
                # FIX: Swapped 'recipient_name' out for 'extracted_name' to prevent NameErrors
                print(
                    f"👤 [GUARDRAIL] Missing address detected for name '{extracted_name}'. Launching background IMAP resolver..."
                )

                # Offload your synchronous IMAP search function safely to a background worker thread
                resolved_address = await asyncio.to_thread(
                    resolve_email_from_name, extracted_name
                )

                if resolved_address:
                    email_address = resolved_address
                else:
                    print(
                        "❌ [GUARDRAIL] Name lookup returned None. Applying fallback address boundaries."
                    )
                    email_address = "unknown@example.com"
        else:
            # If a valid email address string WAS found, use it directly!
            email_address = extracted_email

        # 4. FIX: Enforce baseline safety directly against our local tracking string variable
        if not email_address or email_address == "NOT_FOUND":
            email_address = "unknown@example.com"

        # 5. Populate your final current_email structure securely

        current_email = {
            "from": email_address,
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
    user_query = state.get("user_query")

    print(
        f"🧠 [NODE: fetch_emails] Extracting specific filter variables from query: '{user_query}'"
    )

    # 1. Use your exact, original EmailFilter class inside response_format directly
    try:
        response = await client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Extract search criteria from the user's request matching the schema provided. Default to ALL if read status is not specified.",
                },
                {"role": "user", "content": f"Request: '{user_query}'"},
            ],
            temperature=0.1,
            response_format=EmailFilter,  # <-- Reuses your existing class perfectly
        )
        extracted_filters = response.choices[0].message.parsed
    except Exception as e:
        print(f"⚠️ Filter parsing failed: {e}. Falling back to default filter setup.")
        extracted_filters = EmailFilter()

    # Fallback guard clause if model returns empty payload
    if not extracted_filters:
        extracted_filters = EmailFilter()

    print(
        f"📊 [NODE: fetch_emails] Extracted -> Sender: '{extracted_filters.sender}', Mode: {extracted_filters.mode}, Limit: {extracted_filters.limit}"
    )

    # 2. The standard background worker thread execution block
    def blocking_imap_call():
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        # Build out the query string using the freshly extracted filters object
        criterion = build_imap_criterion(extracted_filters)
        print(f"🔍 [IMAP THREAD] Sending criterion query string: {criterion}")
        status, data = mail.search(None, criterion)

        if not data or not data[0]:
            mail.logout()
            return []

        mail_ids = data[0].split()
        latest_ids = mail_ids[-extracted_filters.limit :][::-1]

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

                display_name, clean_email_address = email.utils.parseaddr(
                    str(msg.get("From", "Unknown"))
                )

                fetched_emails.append(
                    {
                        "id": num.decode("utf-8", errors="ignore"),
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

    # Print menu output to local terminal console window
    print("\n📬 ========== AVAILABLE EMAILS MENU ==========")
    for index, email_item in enumerate(fetched_results):
        print(f"[{index}] FROM: {email_item.get('from')}")
        print(f"    SUBJECT: {email_item.get('subject')}")
        print("-" * 46)
    print("================================================\n")

    # 3. Return BOTH values to state memory permanently!
    return {
        "search_results": fetched_results,
        "email_filters": extracted_filters,  # Saves filters back into AgentState securely
    }


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

agent_app = workflow.compile(checkpointer=memory_checkpointer)
print(
    "⚙️ [SYSTEM LOG] LangGraph Email Workflow compiled successfully without boundary limits."
)
