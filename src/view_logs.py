import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# Set page configuration
st.set_page_config(
    page_title="CPS Pipeline Log Viewer",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Resolve sessions directory paths (checks experiments/sessions first, then data/logs/sessions)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "src" else SCRIPT_DIR

SESSION_DIRS = [
    PROJECT_ROOT / "experiments" / "sessions",
    PROJECT_ROOT / "data" / "logs" / "sessions",
]


def load_session_files():
    """Finds all JSON session logs and returns them sorted by newest first."""
    files = []
    for s_dir in SESSION_DIRS:
        if s_dir.exists():
            files.extend(list(s_dir.glob("*.json")))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def get_status_badge(status: str) -> str:
    """Returns an emoji badge for a given status string."""
    status_upper = (status or "").upper()
    if status_upper == "SUCCESS":
        return "🟢 SUCCESS"
    elif status_upper in ["STAGE2_FAILED", "FAILED"]:
        return "🔴 STAGE2_FAILED"
    elif "ABORTED" in status_upper:
        return "🟡 ABORTED"
    return f"⚪ {status}"


# -----------------------------------------------------------------------------
# SIDEBAR: Session Selection & Summary
# -----------------------------------------------------------------------------
st.sidebar.title("📁 Pipeline Experiments")

session_files = load_session_files()

if not session_files:
    st.sidebar.warning("No session JSON logs found in `experiments/sessions/`.")
    st.info(
        "Run `python src/run_pipeline.py` first to generate experiment session logs!"
    )
    st.stop()

# Build dropdown options formatted with status badges
file_options = {}
for path in session_files:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            status = get_status_badge(data.get("overall_status", "UNKNOWN"))
            timestamp = data.get("timestamp", "").replace("T", " ")[:19]
            label = f"{status} | {path.stem} ({timestamp})"
    except Exception:
        label = f"📄 {path.name}"
    file_options[label] = path

selected_label = st.sidebar.selectbox(
    "Select Session Log:", list(file_options.keys())
)
selected_file = file_options[selected_label]

# Load selected session JSON data
with open(selected_file, "r", encoding="utf-8") as f:
    session_data = json.load(f)

# -----------------------------------------------------------------------------
# MAIN DASHBOARD: Overview Metrics
# -----------------------------------------------------------------------------
st.title("🔍 CPS Pipeline Session Viewer")

status_str = session_data.get("overall_status", "UNKNOWN")
status_badge = get_status_badge(status_str)

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Overall Status", status_badge)
with col2:
    st.metric(
        "Total Latency", f"{session_data.get('total_latency_seconds', 0.0):.2f}s"
    )
with col3:
    stage1_data = session_data.get("stage1_dialogue") or {}
    st.metric("Stage 1 Turns", stage1_data.get("total_turns", 0))
with col4:
    stage2_data = session_data.get("stage2_verification") or {}
    total_attempts = stage2_data.get("total_attempts", 0) if stage2_data else 0
    st.metric("Stage 2 Attempts", total_attempts)

st.caption(
    f"**Session ID:** `{session_data.get('session_id')}` | "
    f"**Model:** `{session_data.get('model', 'N/A')}` | "
    f"**Timestamp:** `{session_data.get('timestamp')}` | "
    f"**World Model:** `{session_data.get('world_model_file') or 'None (Standalone Mode)'}`"
)

st.divider()

# -----------------------------------------------------------------------------
# TABS: Stage 1, Stage 2, World Model, and Raw Telemetry
# -----------------------------------------------------------------------------
tab_stage1, tab_stage2, tab_world, tab_raw = st.tabs(
    [
        "💬 Stage 1: Dialogue Refinement",
        "⚙️ Stage 2: Code Generation & Verification",
        "🌍 World Model Context",
        "📄 Raw JSON",
    ]
)

# --- TAB 1: STAGE 1 DIALOGUE ---
with tab_stage1:
    st.subheader("Stage 1 Conversational Transcript")

    turns = stage1_data.get("dialogue_turns", [])
    if not turns:
        st.info("No dialogue turns recorded for this session.")
    else:
        for turn in turns:
            st.markdown(f"#### Turn {turn.get('turn')}")
            with st.chat_message("user"):
                st.write(turn.get("user_input"))
            with st.chat_message("assistant"):
                st.write(turn.get("assistant_reply"))
            st.write("")

    st.divider()
    st.subheader("Produced Natural Language Refined Prompt")
    refined_prompt = stage1_data.get("refined_prompt")
    if refined_prompt:
        st.info(refined_prompt)
    else:
        st.warning("No refined prompt was synthesized for this session.")

# --- TAB 2: STAGE 2 VERIFICATION ---
with tab_stage2:
    st.subheader("Stage 2 Verification & Generation Loop")

    if not stage2_data:
        st.warning(
            "Stage 2 was not executed for this session (pipeline ended early)."
        )
    else:
        attempts = stage2_data.get("attempts", [])

        if not attempts:
            st.info("No attempt details recorded.")

        for att in attempts:
            att_num = att.get("attempt")
            att_status = att.get("status")
            failed_tier = att.get("failed_tier")

            header_text = f"Attempt {att_num} — {get_status_badge(att_status)}"
            if failed_tier:
                header_text += f" (Failed at: {failed_tier})"

            with st.expander(
                header_text, expanded=(att_status == "SUCCESS" or len(attempts) == 1)
            ):
                if att.get("error_message"):
                    st.error(
                        f"**Error Details ({failed_tier}):**\n\n{att.get('error_message')}"
                    )
                    if (
                        att.get("error_line_no")
                        and att.get("error_line_no") != "-"
                    ):
                        st.caption(
                            f"Line {att.get('error_line_no')}: `{att.get('error_code_line')}`"
                        )

                generated_code = att.get("generated_code")
                if generated_code:
                    st.markdown("**Generated Goal-DSL Code:**")
                    st.code(generated_code, language="text")
                else:
                    st.warning("No code was generated during this attempt.")

        if stage2_data.get("status") == "SUCCESS" and stage2_data.get(
            "generated_code"
        ):
            st.success("🎉 Final Verified Goal-DSL Output:")
            st.code(stage2_data.get("generated_code"), language="text")

# --- TAB 3: WORLD MODEL CONTEXT ---
with tab_world:
    st.subheader("World Model Environment Context")

    world_snapshot = session_data.get("world_model_snapshot")
    world_path_str = session_data.get("world_model_file")

    # Fallback for older logs without a snapshot
    if not world_snapshot and world_path_str:
        w_path = Path(world_path_str)
        if w_path.exists():
            try:
                with open(w_path, "r", encoding="utf-8") as wf:
                    world_snapshot = yaml.safe_load(wf)
            except Exception:
                pass

    if not world_snapshot:
        st.info("No world model context was recorded for this session.")
    else:
        if world_path_str:
            st.caption(f"**Original File Path:** `{world_path_str}`")

        # Broker Settings
        broker_info = world_snapshot.get("broker", {})
        if broker_info:
            st.markdown("#### 🔌 Broker Settings")
            b_cols = st.columns(3)
            b_cols[0].write(f"**Type:** `{broker_info.get('type', 'N/A')}`")
            b_cols[1].write(f"**Host:** `{broker_info.get('host', 'N/A')}`")
            b_cols[2].write(f"**Port:** `{broker_info.get('port', 'N/A')}`")

        # Entities Table
        entities = world_snapshot.get("entities", [])
        if entities:
            st.markdown("#### 🤖 Entities & Devices")
            entity_rows = []
            for e in entities:
                attrs = e.get("attributes", {})
                attr_str = ", ".join(
                    [
                        f"{k}: {v.get('type', 'unknown') if isinstance(v, dict) else v}"
                        for k, v in attrs.items()
                    ]
                )
                entity_rows.append(
                    {
                        "Name": e.get("name"),
                        "Type": e.get("type"),
                        "URI": e.get("uri"),
                        "Source Broker": e.get("source"),
                        "Attributes": attr_str,
                    }
                )
            st.dataframe(pd.DataFrame(entity_rows), use_container_width=True)

        # POIs Table
        pois = world_snapshot.get("pois", [])
        if pois:
            st.markdown("#### 📍 Spatial Topology (POIs)")
            poi_rows = []
            for p in pois:
                coords = p.get("coordinates", {})
                coord_str = (
                    f"({coords.get('x', 0.0)}, {coords.get('y', 0.0)})"
                    if isinstance(coords, dict)
                    else str(coords)
                )
                poi_rows.append(
                    {
                        "POI / Target": p.get("name"),
                        "Room / Zone": p.get("room", "N/A"),
                        "Coordinates (X, Y)": coord_str,
                    }
                )
            st.dataframe(pd.DataFrame(poi_rows), use_container_width=True)

        # Raw YAML Expander
        with st.expander("📄 View Frozen Raw World Model Snapshot (YAML)"):
            st.code(yaml.dump(world_snapshot, sort_keys=False), language="yaml")

# --- TAB 4: RAW JSON ---
with tab_raw:
    st.subheader("Complete Session Telemetry Payload")
    st.json(session_data)