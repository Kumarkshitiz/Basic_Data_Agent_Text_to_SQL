import os, re, time, threading
import duckdb
import sqlglot
from sqlglot import exp
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=types.HttpOptions(timeout=30000),
)
MODEL = os.getenv("GEMINI_MODEL")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL")
LAST_MODEL_USED = None

TABLES = ["Customer", "Invoice", "InvoiceLine", "Track", "Album", "Artist", "Genre"]
ALLOWED = {t.lower() for t in TABLES}

MAX_ROWS = 500        # rows kept from any result
MAX_REPAIRS = 2       # LLM retries after a failed query
QUERY_TIMEOUT = 15    # seconds before a running query is interrupted



def get_con():
    con = duckdb.connect()
    con.sql("INSTALL sqlite; LOAD sqlite;")
    # If READ_ONLY raises an error on your version, remove it; validate_sql still blocks writes.
    con.sql("ATTACH 'Chinook_Sqlite.sqlite' AS chinook (TYPE sqlite, READ_ONLY);")
    con.sql("USE chinook;")
    return con


def build_schema(con):
    parts = []
    for t in TABLES:
        cols = con.sql(f"DESCRIBE {t}").df()
        col_str = ", ".join(f"{r.column_name} {r.column_type}" for r in cols.itertuples())
        sample = con.sql(f"SELECT * FROM {t} LIMIT 3").df().to_string(index=False)
        parts.append(f"Table {t}({col_str})\nSample rows:\n{sample}")
    return "\n\n".join(parts)


def run_sql(con, sql):
    """Plain execution, used by the eval runner for gold SQL."""
    return con.sql(sql).df()


def execute(con, sql):
    """Execute with a timeout and a row cap."""
    timer = threading.Timer(QUERY_TIMEOUT, con.interrupt)
    timer.start()
    try:
        return con.sql(sql).limit(MAX_ROWS + 1).df()
    finally:
        timer.cancel()



def call_llm(prompt, retries=2):
    global LAST_MODEL_USED
    for model in [m for m in (MODEL, FALLBACK_MODEL) if m]:
        for attempt in range(retries + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0),
                )
                LAST_MODEL_USED = model
                return resp.text
            except Exception as e:
                if not any(c in str(e) for c in ("500", "503", "504", "429")):
                    raise  # not transient, don't retry
                time.sleep(3 * 2 ** attempt)
        print(f"[fallback] {model} unavailable, switching")
    raise RuntimeError("All models failed")


def clean_sql(text):
    text = re.sub(r"```(?:sql)?", "", text, flags=re.IGNORECASE).replace("```", "")
    return text.strip().rstrip(";").strip()

RULES = """Rules:
- Return ONLY one SQL query, no explanation, no markdown.
- Use only the tables and columns shown above.
- Money columns (Total, UnitPrice) are DOUBLE. ROUND sums to 2 decimals before ranking or comparing them.
- Only write a single read-only SELECT query (WITH ... SELECT is fine).
- If the question asks for something these tables do not record (for example ratings, listening or play counts, ages, costs, release dates), do NOT approximate it with a different column. Reply with exactly: CANNOT_ANSWER: <one-sentence reason>
- If the question asks to modify data, reply with exactly: CANNOT_ANSWER: only read-only questions are supported
- If the question uses a vague word with no single definition in the data (best, top, popular, biggest, most valuable, recent, good), you MUST put this as the FIRST line of your reply: -- ASSUMPTION: <the definition you used, e.g. best = highest total spend>
- When names can repeat (tracks, albums), group by the ID column, not the name column."""


def condense_question(question, history):
    """Rewrite a follow-up as a standalone question. No history -> unchanged."""
    if not history:
        return question
    convo = "\n".join(f"Q: {h['question']}\nSQL used: {h['sql']}" for h in history[-3:])
    prompt = f"""Rewrite the final question as one standalone question that makes sense without the conversation.
If it is already standalone, return it unchanged.
Keep filters, years, and limits from earlier turns only when the final question clearly builds on them.
Return ONLY the question.

Conversation:
{convo}

Final question: {question}
Standalone question:"""
    return call_llm(prompt).strip()

def generate_sql(question, schema):
    prompt = f"""You write DuckDB SQL for a music store database.

{schema}

{RULES}

Question: {question}
SQL:"""
    return clean_sql(call_llm(prompt))


def repair_sql(question, schema, bad_sql, error):
    prompt = f"""You write DuckDB SQL for a music store database.

{schema}

{RULES}

The query below failed.
Question: {question}
Failed SQL:
{bad_sql}
Error: {error}

Return ONLY the corrected SQL."""
    return clean_sql(call_llm(prompt))


def explain_result(question, sql, df, truncated, assumption):
    table = df.head(20).to_string(index=False)
    note = f"\nAssumption made: {assumption}" if assumption else ""
    prompt = f"""Answer the user's question using ONLY the query result below.

Question: {question}
Result ({len(df)} rows{', truncated' if truncated else ''}; first 20 shown):
{table}{note}

Write 1-3 plain sentences. Use only numbers that appear in the result.
If an assumption was made, state it briefly. Do not mention SQL or tables."""
    return call_llm(prompt).strip()


# --------------------------------------------------------------- validation
ROOTS = tuple(getattr(exp, n) for n in ("Select", "Union", "Except", "Intersect") if hasattr(exp, n))


def validate_sql(sql):
    """Returns (ok, kind, reason).
    kind: 'unsafe' -> refuse, never repair; 'syntax' / 'scope' -> the LLM may repair."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except Exception as e:
        return False, "syntax", f"SQL could not be parsed: {str(e)[:150]}"

    if len(statements) != 1:
        return False, "unsafe", "exactly one statement is allowed"
    tree = statements[0]

    if not isinstance(tree, ROOTS):
        return False, "unsafe", "only read-only SELECT queries are allowed"
    if tree.find(exp.Into):
        return False, "unsafe", "SELECT ... INTO is not allowed"

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            return False, "unsafe", "table functions (e.g. reading files) are not allowed"
        name = t.name.lower()
        if name not in ALLOWED and name not in cte_names:
            return False, "scope", f"table '{t.name}' is not available; use only: {', '.join(TABLES)}"
    return True, None, None


def split_assumption(text):
    m = re.search(r"--\s*ASSUMPTION:\s*(.+)", text or "", re.IGNORECASE)
    return m.group(1).strip() if m else None


# -------------------------------------------------------------------- agent
def ask(question, con, schema, explain=True, history=None):
    """generate -> validate -> execute -> repair (max MAX_REPAIRS) -> explain.
    status: ok | repaired | empty | cannot_answer | refused | failed"""
    out = {"status": None, "answer": None, "sql": None, "df": None,
           "repairs": 0, "assumption": None, "error": None, "model_used": None,
           "standalone": None}
    try:
        standalone = condense_question(question, history)
        out["standalone"] = standalone
        sql = generate_sql(standalone, schema)
        for attempt in range(MAX_REPAIRS + 1):
            if sql.upper().startswith("CANNOT_ANSWER"):
                reason = sql.split(":", 1)[1].strip() if ":" in sql else "not answerable from this data"
                out.update(status="cannot_answer", answer=reason)
                break

            out["sql"] = sql
            out["assumption"] = out["assumption"] or split_assumption(sql)
            ok, kind, reason = validate_sql(sql)

            if not ok and kind == "unsafe":
                out.update(status="refused", error=reason, answer=f"Blocked: {reason}.")
                break

            error = reason
            if ok:
                try:
                    df = execute(con, sql)
                    truncated = len(df) > MAX_ROWS
                    df = df.head(MAX_ROWS)
                    out["df"] = df
                    out["status"] = "repaired" if out["repairs"] else "ok"
                    if len(df) == 0:
                        out["status"] = "empty"
                        out["answer"] = "The query ran but returned no rows."
                    elif explain:
                        try:
                            out["answer"] = explain_result(standalone, sql, df, truncated, out["assumption"])
                        except Exception:
                            out["answer"] = "Query ran successfully; see the table below."
                    break
                except Exception as e:
                    error = str(e)[:300]

            out["error"] = error
            if attempt == MAX_REPAIRS:
                out.update(status="failed",
                           answer="I couldn't produce a working query for that question.")
                break
            sql = repair_sql(standalone, schema, sql, error)
            out["repairs"] += 1
    except Exception as e:
        out.update(status="failed", error=str(e)[:300],
                   answer="Something went wrong while contacting the model.")
    out["model_used"] = LAST_MODEL_USED
    return out

if __name__ == "__main__":
    con = get_con()
    schema = build_schema(con)
    history = []
    while True:
        q = input("\nAsk a question ('q' to quit, 'new' to reset context): ").strip()
        if q.lower() == "q":
            break
        if q.lower() == "new":
            history = []
            print("Context cleared.")
            continue
        if not q:
            continue
        t = time.time()
        r = ask(q, con, schema, history=history)
        if r["standalone"] and r["standalone"] != q:
            print(f"(understood as: {r['standalone']})")
        print(f"\n{r['answer']}")
        if r["assumption"]:
            print(f"(assumption: {r['assumption']})")
        if r["sql"]:
            print("\nSQL:", r["sql"])
        if r["df"] is not None and len(r["df"]):
            print(r["df"].head(20).to_string(index=False))
        if r["status"] in ("ok", "repaired", "empty"):
            history.append({"question": q, "sql": r["sql"]})
        print(f"\n[status={r['status']} repairs={r['repairs']} "
              f"model={r['model_used']} {time.time() - t:.1f}s]")