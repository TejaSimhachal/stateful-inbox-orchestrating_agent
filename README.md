# stateful-inbox-orchestrating_agent
# Email Automation Agent with LangGraph and FastAPI

This project implements an advanced, event-driven AI Agent pipeline designed for email automation, featuring robust state persistence, human-in-the-loop capabilities, and asynchronous operations.

## ⚙️ System Architecture & Data Flow

The backend of this system is structured into two main components:
1.  **FastAPI REST API**: A high-performance presentation layer responsible for handling client requests.
2.  **LangGraph Framework**: A stateful execution orchestration framework that manages the AI agent's workflow.

The system utilizes a transactional persistence layer (`MemorySaver`) to manage long-running, multi-turn asynchronous loops. This allows execution frames to freeze and resume cleanly via unique user session tokens (`thread_id`), ensuring continuity in complex interactions.

### 🔄 End-to-End Architectural Data Flow

The following diagram illustrates the interaction boundaries between the REST API endpoints, the underlying LangGraph execution topology, and external mail protocols (IMAP/SMTP):

```
[ CLIENT HTTP REQ ]
     │
     ▼
┌────────────────────────────────────────────────────────────────────────┐
│ FastAPI API Gate Controllers (main.py)                                 │
│                                                                        │
│   POST /agent/start      ──► Initial State Ingestion                   │
│   POST /agent/select     ──► Pointer Mutation via .aupdate_state()     │
│   POST /agent/review     ──► Frame Resumption via Command(resume=...)  │
└──────────────────────────────────┬─────────────────────────────────────┘
     │Streams State via .astream()│
     ▼
┌────────────────────────────────────────────────────────────────────────┐
│ LangGraph Stateful Agent Execution Engine (imap_fastapi.py)            │
│                                                                        │
│           ┌─────────────── entrypoint_router ──────────────┐           │
│           │ (If state["draft_reply"] exists, auto-bypass)  │           │
│           ▼                                                ▼           │
│   [ fetch_emails_node ]                             [ generate_draft ] ◄──┐
│           │                                                │              │
│  Executes Task Isolation                                   ▼              │
│  via asyncio.to_thread                               [ human_review ]     │
│           │                                                │              │
│    (Connects to IMAP)                                      ▼              │
│           │                                         Is state approved?    │
│           ▼                                         ├── NO ───────────────┘
│         [ END ] (State Yielded to UI)               └── YES ──────┐
│                                                                   ▼
│                                                           [ transmit_smtp ]
│                                                                   │
│                                                                   ▼
│                                                                 [ END ]
└────────────────────────────────────────────────────────────────────────┘
```

## 🧠 Core Engineering Highlight Columns

#### 1. Transactional State Persistence Contract (`AgentState`)
All communication, context retention, and history are governed by an atomic data contract schema. Every node operation executes mutations cleanly against this thread-locked schema structure:

| Key State Field    | Data Type            | System Governance                                                               |
| :----------------- | :------------------- | :------------------------------------------------------------------------------ |
| `user_query`       | `str`                | Holds primary search constraints or volatile human critique feedback loops.     |
| `search_results`   | `List[Dict[str, Any]]` | Structured storage for email headers, metadata, and body packets pulled from IMAP. |
| `current_idx`      | `Optional[int]`      | Explicit index tracking boundary to isolate the specific selected conversation loop. |
| `current_email`    | `Dict[str, Any]`     | The locked, schema-validated target context block containing clean parsed addresses. |
| `draft_reply`      | `str`                | Volatile text scratchpad storing the active response iteration draft.           |
| `approved`         | `bool`               | Evaluation gate boolean controlling final downstream email delivery.            |

#### 2. Non-Blocking IO Isolation Strategy
Network handshakes via legacy mail protocols (`imaplib` / `smtplib`) are natively synchronous and blocking. To prevent event-loop starvation and ensure the FastAPI server never slows down, these transactions are isolated on a specialized thread pool:

```python
# Thread isolation pattern deployed across data nodes
fetched_results = await asyncio.to_thread(blocking_imap_call)
```

#### 3. State-Aware Contextual Revision Routing
To support iterative feedback, the `entrypoint_router` inspects the active thread checkpoint before querying the LLM. If `state["draft_reply"]` contains text data, the router identifies this run as an ongoing human critique iteration. It bypasses token classification entirely and routes directly to the drafting environment, ensuring previous context is never erased.

#### 4. Asynchronous Human-in-the-Loop (HITL) Gateways
The execution engine stops completely when human review is required. LangGraph saves the active frame to the database checkpointer using `interrupt()`, drops the execution token, and returns a `paused_for_review` code to the client. The graph can sleep in this state indefinitely. It only wakes up when the supervisor hits the `/agent/review` route, which uses the `Command` framework to inject the decision payload back into the code block:

```python
# Atomic state injection to wake up the execution graph
await agent_app.astream(Command(resume=review_payload), config)
```