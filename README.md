 # Email Automation Agent with LangGraph and FastAPI

This project implements an advanced, event-driven AI Agent pipeline designed for email automation. It combines a FastAPI HTTP control surface with a stateful LangGraph execution topology to support multi-turn workflows, human-in-the-loop review, and transactional state persistence so long-running asynchronous runs can pause and resume.

## Quick summary

- Purpose: Automate email fetch, draft generation, human review, and SMTP transmission while preserving execution state between asynchronous turns.
- Primary files:
  - `main.py` — FastAPI controllers that start, resume, inspect, and mutate the agent execution state.
  - `email_agent_graph.py` — LangGraph StateGraph definition: nodes, routers, checkpointer (MemorySaver), and `agent_app` compiled instance.
  - `.env.example` — example environment variables used for model credentials and email access.
  - `requirements.txt` — Python package dependencies.

## Architecture & data flow

Two main components work together:
1. FastAPI REST API (main.py) — presentation and control layer. It accepts client requests, inspects saved agent frames, injects commands, and resumes the LangGraph execution.
2. LangGraph Stateful Execution (email_agent_graph.py) — the stateful workflow that runs nodes such as `fetch_emails`, `generate_draft`, `human_review`, and `transmit_smtp`.

The system uses a transactional persistence layer (`MemorySaver`) so execution frames can freeze and resume via `agent_app.astream()` and `Command(resume=...)`. Blocking IMAP/SMTP calls run on executor threads to avoid blocking the event loop.

```
[ CLIENT HTTP REQ ]
     │
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ FastAPI API Gate Controllers (main.py)                               │
│                                                                       │
│   POST /agent/start      ──► Initial State Ingestion                  │
│   POST /agent/select     ──► Pointer Mutation via .aupdate_state()    │
│   POST /agent/review     ──► Frame Resumption via Command(resume=...) │
└──────────────────────────────────┬──────────────────────────────────┘
     │Streams State via .astream()│
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LangGraph Stateful Agent Execution Engine (email_agent_graph.py)     │
│                                                                       │
│           ┌─────────────── entrypoint_router ──────────────┐          │
│           │ (If state["draft_reply"] exists, auto-bypass)  │          │
│           ▼                                                ▼          │
│   [ fetch_emails_node ]                             [ generate_draft ]◄─┐
│           │                                                │            │
│  Executes Task Isolation                                   ▼            │
│  via asyncio.to_thread                               [ human_review ]    │
│           │                                                │            │
│    (Connects to IMAP)                                      ▼            │
│           │                                         Is state approved?   │
│           ▼                                         ├── NO ──────────────┘
│         [ END ] (State Yielded to UI)               └── YES ──────┐
│                                                                  ▼
│                                                          [ transmit_smtp ]
│                                                                  │
│                                                                  ▼
│                                                                [ END ]
└─────────────────────────────────────────────────────────────────────┘
```

## Core engineering highlights

- Transactional state contract (AgentState): the workflow uses a typed state dict containing `user_query`, `search_results`, `current_idx`, `current_email`, `draft_reply`, `approved`, and `email_filters` so nodes can read and mutate the shared execution frame atomically.

- Non-blocking IO isolation: legacy IMAP/SMTP (imaplib / smtplib) are synchronous; the code runs these calls via `asyncio.to_thread()` to avoid event-loop starvation.

- State-aware routing: `entrypoint_router` inspects the active frame — if `draft_reply` exists it's a revision pass and routes directly to `generate_draft` to avoid unnecessary reclassification.

- Human-in-the-loop gate: `human_review_node` uses `interrupt()` to freeze execution and waits for a `Command(resume=...)` from the API, enabling safe human review and revisions.

## Files and responsibilities

- `main.py` — exposes these endpoints:
  - POST /agent/start
  - POST /agent/select/{thread_id}/{index_number}
  - POST /agent/review
  - GET /agent/state/{thread_id}

  It uses `agent_app.astream()`, `agent_app.aget_state()`, and `agent_app.aupdate_state()` to run and manipulate the LangGraph execution.

- `email_agent_graph.py` — defines:
  - AgentState TypedDict and `EmailFilter` Pydantic model (with date validation for IMAP criteria).
  - Nodes: `fetch_emails_node`, `generate_draft_node`, `human_review_node`, `transmit_smtp_node`.
  - Routers: `entrypoint_router` (entrypoint selection) and `review_edge_router` (decide between continue drafting and SMTP transmit).
  - Workflow composition: nodes, edges, conditional routers, and `MemorySaver` checkpointer.

- `.env.example` — variables you must set to run the app. Copy to `.env` and populate real credentials.

## Configuration / environment

Copy the example and provide real credentials before running:

```
cp .env.example .env
# then edit .env and fill in values
```

Environment variables used by the code:
- GITHUB_TOKEN — used as the API key for AsyncOpenAI in the code (the repo uses this env var name; update if you prefer a different name).
- EMAIL_USER — Gmail account used for IMAP/SMTP.
- EMAIL_PASS — Gmail app password (16-character app password expected for Gmail SMTP/IMAP use).

Security note: do not commit `.env` or credentials to the repository.

## Quick Start

1.  **Start the FastAPI server**: In your terminal, navigate to the project directory and run:
    ```bash
    python main.py
    ```

2.  **Run the Streamlit application**: In a *separate* terminal, also in the project directory, execute:
    ```bash
    streamlit run streamlit_app.py
    ```

    This will open the Streamlit UI in your web browser, allowing you to interact with the email agent.


## HTTP API examples

Start a new agent run:

```
POST /agent/start
Content-Type: application/json
{
  "thread_id": "thread-123",
  "user_query": "Find unread invoices from Acme Corp"
}
```

Select an email and request a reply draft:

```
POST /agent/select/thread-123/0
Content-Type: application/json
{
  "reply_instruction": "Please write a polite request for invoice and due date information."
}
```

Submit human review (approve or request revision):

```
POST /agent/review
Content-Type: application/json
{
  "thread_id": "thread-123",
  "decision": "revise",
  "feedback": "Shorten the reply and add a sentence about preferred payment methods."
}
```

Inspect current saved state:

```
GET /agent/state/thread-123
```

## Notes & recommended improvements

- The code currently hardcodes the model name (`gpt-4o-mini`) and the AsyncOpenAI base_url to `https://models.inference.ai.azure.com`. Consider moving model name and base_url into environment variables for configuration flexibility.

- The README previously referenced `imap_fastapi.py` in one spot; this repository uses `email_agent_graph.py`. Consider renaming or updating references for consistency.

- For production use, prefer a persistent checkpointer (database-backed) instead of `MemorySaver` so agent frames survive process restarts.

- Add tests for the API endpoints and small integration tests that mock IMAP/SMTP to validate the workflow logic.

## License

This repository does not currently include a LICENSE file — add one if you intend to publish the project under an open-source license.

---

Maintained explanation: the original README's architecture, state model, non-blocking IO strategy, and human-in-the-loop flow have been preserved and reorganized for clarity. If you'd like, I can now push additional changes such as renaming in-repo references, moving configuration into `.env`, or adding a Dockerfile and CI workflow.
