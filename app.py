import json, time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from agent import ask, get_con, build_schema

LOG_PATH = Path("query_log.jsonl")
TIME_COLS = {"year", "yr", "month", "mth", "date"}
HISTORY_STATUSES = ("ok", "repaired", "empty")

st.set_page_config(page_title="Chinook Data Agent", page_icon="🎵", layout="wide")


@st.cache_resource
def load():
    con = get_con()
    return con, build_schema(con)


def log_query(question, r, latency):
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "standalone": r.get("standalone"),
        "sql": r.get("sql"),
        "status": r.get("status"),
        "repairs": r.get("repairs"),
        "assumption": r.get("assumption"),
        "rows": None if r.get("df") is None else len(r["df"]),
        "latency_s": round(latency, 2),
        "model": r.get("model_used"),
        "error": r.get("error"),
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_log():
    if not LOG_PATH.exists():
        return pd.DataFrame()
    rows = [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return pd.DataFrame(rows)


def auto_chart(df):
    """Bar chart for category + number(s); line chart when the first column is a year/month."""
    if df is None or df.shape[1] < 2 or not (2 <= len(df) <= 30):
        return
    first = df.columns[0]
    nums = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not nums:
        return
    try:
        if pd.api.types.is_numeric_dtype(df[first]):
            if str(first).lower() in TIME_COLS:
                st.line_chart(df.set_index(first)[nums])
        else:
            st.bar_chart(df.set_index(first)[nums])
    except Exception:
        pass  # a chart is a nice-to-have, never block the answer


def render(question, r, latency=None):
    status = r["status"]
    if r.get("standalone") and r["standalone"].strip() != question.strip():
        st.caption(f"Understood as: {r['standalone']}")

    answer = r.get("answer") or ""
    if status in ("refused", "cannot_answer", "failed"):
        st.warning(answer)
    elif status == "empty":
        st.info(answer)
    else:
        st.write(answer)

    if r.get("assumption"):
        st.caption(f"Assumption: {r['assumption']}")

    if r.get("sql") and status != "cannot_answer":
        with st.expander("SQL used"):
            st.code(r["sql"], language="sql")

    df = r.get("df")
    if df is not None and len(df):
        st.dataframe(df, width="stretch", hide_index=True)
        auto_chart(df)

    bits = [f"status: {status}", f"repairs: {r.get('repairs', 0)}", f"model: {r.get('model_used')}"]
    if latency is not None:
        bits.append(f"{latency:.1f}s")
    st.caption(" · ".join(bits))


# ------------------------------------------------------------------ state
if "messages" not in st.session_state:
    st.session_state.messages = []   # [{"question", "result", "latency"}]
if "history" not in st.session_state:
    st.session_state.history = []    # [{"question", "sql"}] for follow-ups

con, schema = load()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Chinook Data Agent")
    st.caption("Ask questions about a music store database in plain English.")
    if st.button("New chat", width="stretch"):
        st.session_state.messages = []
        st.session_state.history = []
        st.rerun()

    log = read_log()
    if len(log):
        st.subheader("Query log")
        st.metric("Queries", len(log))
        st.metric("Avg latency", f"{log['latency_s'].mean():.1f}s")
        st.metric("Needed repair", f"{100 * (log['repairs'] > 0).mean():.0f}%")
        st.bar_chart(log["status"].value_counts())

# ------------------------------------------------------------------- chat
st.title("🎵 Chinook Data Agent")

if not st.session_state.messages:
    st.markdown(
        "Try: *Top 5 billing countries by revenue*, then follow up with *and only for 2012?*"
    )

for m in st.session_state.messages:
    with st.chat_message("user"):
        st.write(m["question"])
    with st.chat_message("assistant"):
        render(m["question"], m["result"], m["latency"])

q = st.chat_input("Ask a question about the data")
if q:
    with st.chat_message("user"):
        st.write(q)
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            t0 = time.time()
            result = ask(q, con, schema, history=st.session_state.history)
            latency = time.time() - t0
        render(q, result, latency)

    st.session_state.messages.append({"question": q, "result": result, "latency": latency})
    if result["status"] in HISTORY_STATUSES:
        st.session_state.history.append({"question": q, "sql": result["sql"]})
    log_query(q, result, latency)