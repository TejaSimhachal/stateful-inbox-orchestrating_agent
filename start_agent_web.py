import streamlit as st
import requests
import json
import uuid

# --- Configuration ---
FASTAPI_BASE_URL = (
    "http://127.0.0.1:8000"  # Ensure this matches your FastAPI server's address
)
# --- Streamlit Session State Initialization ---
# Initialize session state variables if they don't exist
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "workflow_stage" not in st.session_state:
    # Possible stages: 'idle', 'start_query', 'fetching', 'selection', 'review', 'completed'
    st.session_state.workflow_stage = "idle"
if "fetched_emails" not in st.session_state:
    st.session_state.fetched_emails = []
if "current_email_info" not in st.session_state:
    st.session_state.current_email_info = {}
if "current_draft" not in st.session_state:
    st.session_state.current_draft = ""
if "current_recipient" not in st.session_state:
    st.session_state.current_recipient = ""
if "last_status" not in st.session_state:
    st.session_state.last_status = "Idle"
if "error_message" not in st.session_state:
    st.session_state.error_message = None
if "full_agent_state" not in st.session_state:
    st.session_state.full_agent_state = {}


# --- Helper Function for API Calls ---
def _call_api(method, endpoint, json_data=None):
    try:
        url = f"{FASTAPI_BASE_URL}{endpoint}"
        if method == "POST":
            response = requests.post(url, json=json_data)
        elif method == "GET":
            response = requests.get(url)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.ConnectionError:
        st.session_state.error_message = (
            "Could not connect to FastAPI server. Is it running?"
        )
        return None
    except requests.exceptions.RequestException as e:
        st.session_state.error_message = f"API Request failed: {e}"
        if "response" in locals() and response is not None:
            st.session_state.error_message += f"\nResponse: {response.text}"
        return None


# --- API Interaction Functions ---
def start_agent(user_query):
    st.session_state.error_message = None
    # Generate a unique thread_id for each new session
    new_thread_id = str(uuid.uuid4())
    data = {"thread_id": new_thread_id, "user_query": user_query}
    st.session_state.workflow_stage = "start_query"

    response = _call_api("POST", "/agent/start", json_data=data)
    if response:
        st.session_state.thread_id = new_thread_id
        st.session_state.last_status = response.get("status", "Unknown")
        st.session_state.fetched_emails = []
        st.session_state.current_draft = ""
        st.session_state.current_recipient = ""
        st.session_state.current_email_info = {}
        st.session_state.full_agent_state = response.get("all_saved_data", {})

        if response.get("status") == "paused_for_review":
            st.session_state.workflow_stage = "review"
            review_data = response.get("data_to_review", {})
            st.session_state.current_draft = review_data.get("draft_to_verify", "")
            st.session_state.current_recipient = review_data.get("recipient", "")
        elif (
            response.get("next_waiting_node")
            and "fetch_emails" in response["next_waiting_node"]
        ):
            st.session_state.workflow_stage = "fetching"
        else:
            st.session_state.workflow_stage = (
                "completed"  # For direct compose that finishes
            )

        st.success(
            f"Agent started with Thread ID: {st.session_state.thread_id}. Status: {st.session_state.last_status}"
        )
        st.rerun()


def get_agent_state():
    if st.session_state.thread_id:
        st.session_state.error_message = None
        response = _call_api("GET", f"/agent/state/{st.session_state.thread_id}")
        if response:
            # Update status and relevant data from the full state
            next_waiting_node = response.get("next_waiting_node")
            if next_waiting_node:
                st.session_state.last_status = next_waiting_node[0]
            else:
                # If next_waiting_node is empty or None, assume completed or idle
                st.session_state.last_status = (
                    "Completed"  # Or "Idle" depending on desired default
                )

            all_saved_data = response.get("all_saved_data", {})
            st.session_state.full_agent_state = all_saved_data
            st.session_state.fetched_emails = all_saved_data.get("search_results", [])
            st.session_state.current_draft = all_saved_data.get("draft_reply", "")
            st.session_state.current_email_info = all_saved_data.get(
                "current_email", {}
            )
            st.session_state.current_recipient = all_saved_data.get(
                "current_email", {}
            ).get("from", "")

            # Update workflow stage based on fetched state
            if st.session_state.last_status == "human_review":
                st.session_state.workflow_stage = "review"
            elif st.session_state.last_status == "fetch_emails":
                st.session_state.workflow_stage = "fetching"
                if (
                    st.session_state.fetched_emails
                ):  # If emails were fetched and it's waiting for selection
                    st.session_state.workflow_stage = "selection"
            elif (
                st.session_state.last_status == "transmit_smtp"
                or st.session_state.last_status == "Completed"
            ):
                st.session_state.workflow_stage = "completed"
            else:
                st.session_state.workflow_stage = "idle"  # Default or completed state

            st.info("Agent state refreshed.")
            st.rerun()


def select_email(index_number, reply_instruction):
    if st.session_state.thread_id:
        st.session_state.error_message = None
        data = {"reply_instruction": reply_instruction}
        response = _call_api(
            "POST",
            f"/agent/select/{st.session_state.thread_id}/{index_number}",
            json_data=data,
        )
        if response:
            st.session_state.current_draft = response.get("generated_draft", "")
            st.session_state.current_recipient = response.get("stored_recipient", "")
            st.session_state.last_status = "Draft Generated, Awaiting Review"
            st.session_state.workflow_stage = "review"
            st.success("Email selected and draft generation initiated.")
            st.rerun()


def submit_review(decision, feedback=None):
    if st.session_state.thread_id:
        st.session_state.error_message = None
        data = {"thread_id": st.session_state.thread_id, "decision": decision}
        if feedback:
            data["feedback"] = feedback

        response = _call_api("POST", "/agent/review", json_data=data)
        if response:
            # After review submission, immediately refresh state to get the result
            # This is crucial for the revision loop to show the new draft
            st.success(
                f"Review submitted: {decision.capitalize()}. Refreshing agent state..."
            )
            get_agent_state()  # Will rerun and update workflow_stage and draft
        else:
            st.error("Failed to submit review.")


# --- Streamlit UI Pages ---
def home_page():
    st.header("🚀 Start New Email Agent Workflow")

    if st.session_state.workflow_stage == "idle" or st.button(
        "Start New Workflow", key="start_new_btn"
    ):
        st.session_state.workflow_stage = "idle"  ## refershes.
        user_query = st.text_input(
            "Enter your initial query or instruction for the email agent:",
            placeholder="e.g., Find unread invoices from Acme Corp"
            or "Compose a thank you email to John Doe",
            key="user_query_input",
        )

        if st.button("Initiate Agent Workflow", key="initiate_workflow_btn"):
            if user_query:
                start_agent(user_query)
            else:
                st.warning("Please enter a query to start the agent.")


def email_selection_page():
    st.header("📥 Email Selection & Draft Generation")

    if not st.session_state.thread_id:
        st.info("Please start an agent workflow on the 'Home' page first.")
        return

    st.write(f"Current Thread ID: **{st.session_state.thread_id}**")

    # Automatically refresh state if in fetching stage and emails are not yet displayed
    if (
        st.session_state.workflow_stage == "fetching"
        and not st.session_state.fetched_emails
    ):
        st.info("Agent is fetching emails... Automatically refreshing state.")
        get_agent_state()  # This will trigger a rerun if state changes
        return  # Exit to let the rerun take effect

    if st.button("Refresh Agent State & Emails", key="refresh_emails_btn"):
        get_agent_state()

    if st.session_state.fetched_emails:
        st.subheader("Fetched Emails:")
        # Display emails in a DataFrame for better readability
        emails_df = [
            {
                "Index": i,
                "From": e.get("from", ""),
                "Subject": e.get("subject", ""),
                "Date": e.get("date", ""),
            }
            for i, e in enumerate(st.session_state.fetched_emails)
        ]
        st.dataframe(emails_df)

        selected_index = st.selectbox(
            "Select an email to reply to (by Index):",
            options=list(range(len(st.session_state.fetched_emails))),
        )
        reply_instruction = st.text_area(
            "Enter instructions for the reply:",
            placeholder="e.g., Please write a polite request for invoice and due date information.",
        )

        if st.button("Generate Reply Draft", key="generate_draft_btn"):
            if selected_index is not None and reply_instruction:
                select_email(selected_index, reply_instruction)
            else:
                st.warning("Please select an email and provide reply instructions.")
    elif st.session_state.workflow_stage == "fetching":
        st.info(
            "Agent is currently fetching emails. Refresh in a moment if not displayed."
        )
    else:
        st.info(
            "No emails fetched yet. Start a workflow on the 'Home' page with a query like 'Find my unread emails'."
        )


def human_review_page():
    st.header("👀 Human Review & Approval")

    if not st.session_state.thread_id or st.session_state.workflow_stage != "review":
        st.info(
            "No draft available for review. Start a workflow or generate one from the 'Email Selection & Draft' page."
        )
        return

    st.write(f"Current Thread ID: **{st.session_state.thread_id}**")

    if st.session_state.current_draft:
        st.subheader("Draft for Review:")
        st.markdown(f"**Recipient**: {st.session_state.current_recipient}")
        st.text_area("Email Draft:", st.session_state.current_draft, height=300)

        feedback = st.text_area(
            "Provide feedback for revision (optional):",
            placeholder="e.g., Shorten the reply and add a sentence about preferred payment methods.",
            key="review_feedback_text",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Approve and Send", key="approve_btn"):
                submit_review("approve")
        with col2:
            if st.button("Request Revision", key="revise_btn"):
                submit_review("revise", feedback if feedback else "Please revise.")
    else:
        st.info(
            "No draft available for review. This might happen if the agent is still processing or has completed."
        )
        if st.button("Refresh Agent State", key="refresh_review_state_btn"):
            get_agent_state()


def agent_state_page():
    st.header("📊 Agent State & History")

    if not st.session_state.thread_id:
        st.info("No active agent session. Start one on the 'Home' page.")
        return

    st.write(f"Current Thread ID: **{st.session_state.thread_id}**")

    if st.button("Refresh Full Agent State", key="refresh_full_state_btn"):
        get_agent_state()

    st.subheader("Latest Agent Status")
    st.write(f"**Status**: {st.session_state.last_status}")
    st.write(f"**Workflow Stage**: {st.session_state.workflow_stage}")
    st.write(f"**Draft Available**: {bool(st.session_state.current_draft)}")
    st.write(f"**Emails Fetched**: {len(st.session_state.fetched_emails)}")

    st.subheader("Full State Data")
    st.json(st.session_state.full_agent_state)


# --- Main Streamlit App Layout ---
st.sidebar.title("📧 Email Agent Dashboard")

# Dynamic page selection based on workflow stage
page_options = {
    "idle": "Home",
    "start_query": "Home",
    "fetching": "Email Selection & Draft",
    "selection": "Email Selection & Draft",
    "review": "Human Review",
    "completed": "Agent State",  # Show final state
}

# Default to 'Home' if workflow_stage is not in options or if no thread_id
initial_page_key = page_options.get(st.session_state.workflow_stage, "Home")

# If thread_id exists, always offer all pages, but highlight the current one
if st.session_state.thread_id:
    sidebar_selection = st.sidebar.radio(
        "Go to",
        ["Home", "Email Selection & Draft", "Human Review", "Agent State"],
        index=(
            ["Home", "Email Selection & Draft", "Human Review", "Agent State"].index(
                initial_page_key
            )
        ),
    )
else:
    sidebar_selection = st.sidebar.radio("Go to", ["Home"], index=0)

# Display global error message if any
if st.session_state.error_message:
    st.error(st.session_state.error_message)

# Render selected page
if sidebar_selection == "Home":
    home_page()
elif sidebar_selection == "Email Selection & Draft":
    email_selection_page()
elif sidebar_selection == "Human Review":
    human_review_page()
elif sidebar_selection == "Agent State":
    agent_state_page()

st.sidebar.markdown("---")
st.sidebar.write("**Current Session Info**")
st.sidebar.write(f"Thread ID: {st.session_state.thread_id or 'N/A'}")
st.sidebar.write(f"Status: {st.session_state.last_status}")
st.sidebar.write(f"Workflow Stage: {st.session_state.workflow_stage}")

# streamlit run start_agent_web.py
