from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Literal, Any
from langgraph.types import Command
from email_agent_graph import agent_app  # Ensure your compiled graph is here
import uvicorn

app = FastAPI(title="Email Agent Controller API")


# --- PYDANTIC SCHEMAS ---
class StartAgentRequest(BaseModel):
    thread_id: str
    user_query: str


class SubmitReviewRequest(BaseModel):
    thread_id: str
    decision: Literal["approve", "revise"]
    feedback: Optional[str] = None


class SelectionRequest(BaseModel):
    reply_instruction: str


class AgentResponse(BaseModel):
    status: str
    thread_id: str
    next_node: Optional[List[str]] = None
    data_to_review: Optional[Any] = None


# --- ENDPOINTS ---
@app.post("/agent/start", response_model=AgentResponse)
async def start_agent_workflow(request: StartAgentRequest):
    config = {"configurable": {"thread_id": request.thread_id}}

    # Complete, safe initialization contract
    initial_state = {
        "user_query": request.user_query,
        "messages": [],
        "search_results": [],
        "current_idx": None,  # <-- INITIATED SAFELY AS NONE
        "current_email": {},
        "draft_reply": "",
        "approved": False,
        "email_filters": None,
    }

    async for _ in agent_app.astream(initial_state, config, stream_mode="updates"):
        pass

    current_state = await agent_app.aget_state(config)

    if current_state.tasks:
        active_task = current_state.tasks[0]
        interrupt_details = getattr(active_task, "interrupts", [])
        review_data = (
            getattr(interrupt_details[0], "value", interrupt_details[0])
            if interrupt_details
            else None
        )

        return AgentResponse(
            status="paused_for_review",
            thread_id=request.thread_id,
            next_node=list(current_state.next),
            data_to_review=review_data,
        )

    return AgentResponse(status="completed", thread_id=request.thread_id)


@app.post("/agent/review")
async def submit_human_review(request: SubmitReviewRequest):
    config = {"configurable": {"thread_id": request.thread_id}}

    pre_state = await agent_app.aget_state(config)
    print(f"🔍 Current Email validation state: {pre_state.values.get('current_email')}")

    review_payload = {"decision": request.decision, "feedback": request.feedback}

    # Resume execution flow using structural Command injection
    async for _ in agent_app.astream(
        Command(resume=review_payload), config, stream_mode="updates"
    ):
        pass

    return {"status": "execution_pass_complete", "thread_id": request.thread_id}


# FIX: Explicitly added thread_id to the path parameters array
@app.post("/agent/select/{thread_id}/{index_number}")
async def select_email_by_index(
    thread_id: str, index_number: int, request: SelectionRequest
):
    config = {"configurable": {"thread_id": thread_id}}

    current_state = await agent_app.aget_state(config)
    fetched_emails = current_state.values.get("search_results", [])

    if not fetched_emails or index_number < 0 or index_number >= len(fetched_emails):
        raise HTTPException(status_code=400, detail="Requested index is out of bounds.")

    # Overwrite state memory to set the index and pass the instructions
    await agent_app.aupdate_state(
        config,
        {
            "current_idx": index_number,  # <-- SETS THE STATE POINTER!
            "user_query": request.reply_instruction,
            "draft_reply": "",
        },
        as_node="fetch_emails",
    )

    # Run the graph starting from generate_draft with our new state configuration
    async for event in agent_app.astream(
        {"user_query": request.reply_instruction, "current_idx": index_number},
        config,
        stream_mode="updates",
    ):
        print(f"🔄 Graph Processing Run: {event}")

    updated_state = await agent_app.aget_state(config)
    return {
        "status": "paused_for_review",
        "generated_draft": updated_state.values.get("draft_reply"),
        "stored_recipient": updated_state.values.get("current_email", {}).get("from"),
    }


@app.get("/agent/state/{thread_id}")
async def check_active_agent_state(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    current_state = await agent_app.aget_state(config)

    return {
        "thread_id": thread_id,
        "next_waiting_node": list(current_state.next),
        "fetched_emails_count": len(current_state.values.get("search_results", [])),
        "all_saved_data": current_state.values,
    }


if __name__ == "__main__":
    # Ensure this file is saved as main.py for this loader string to operate
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
