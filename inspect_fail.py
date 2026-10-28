import json, sys
from agent import get_con, run_sql


def loose(df):
    """Compare values only: ignores column names AND column order."""
    rows = []
    for row in df.itertuples(index=False):
        vals = [round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
                for v in row]
        rows.append(tuple(sorted(vals, key=str)))
    return sorted(rows)


qid = int(sys.argv[1])
tag = sys.argv[2] if len(sys.argv) > 2 else "baseline"

con = get_con()
results = json.load(open(f"results_{tag}.json"))["results"]
evals = {e["id"]: e for e in json.load(open("evals.json"))}
r = next(x for x in results if x["id"] == qid)

print(f"Q{qid}: {evals[qid]['question']}")
print(f"passed: {r['passed']}  model: {r.get('model_used')}  error: {r['error']}")
print("\nMODEL SQL:\n", r["sql"])

if not r["sql"]:
    sys.exit("\n(no SQL was generated)")

try:
    got = run_sql(con, r["sql"])
except Exception as e:
    sys.exit(f"\nMODEL SQL FAILED: {e}")

exp = run_sql(con, evals[qid]["gold_sql"])

print("\nMODEL columns:   ", list(got.columns), "rows:", len(got))
print("EXPECTED columns:", list(exp.columns), "rows:", len(exp))
print("\nMODEL RESULT:\n", got.head(8))
print("\nEXPECTED:\n", exp.head(8))
print("\nSame values ignoring column order/names:", loose(got) == loose(exp))