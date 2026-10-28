import argparse, json, math, time
import agent
from agent import get_con, build_schema, generate_sql, run_sql


def norm_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return "NULL"
        return round(float(v), 2)
    return str(v)


def normalize(df):
    """Order-insensitive, column-name-insensitive, rounds numbers to 2 decimals."""
    rows = [tuple(norm_value(v) for v in row) for row in df.itertuples(index=False)]
    return sorted(rows, key=str)


def run_once(con, schema, evals, expected, run_no, total_runs, loop):
    results = []
    for e in evals:
        sql, error, passed, model_used = "", None, False, None
        status, repairs = None, 0
        try:
            if loop:
                res = agent.ask(e["question"], con, schema, explain=False)
                sql, model_used = res["sql"] or "", res["model_used"]
                status, repairs = res["status"], res["repairs"]
                if res["df"] is None:
                    raise RuntimeError(f"{res['status']}: {res['error'] or res['answer']}")
                actual = normalize(res["df"])
            else:
                sql = generate_sql(e["question"], schema)
                model_used = agent.LAST_MODEL_USED
                actual = normalize(run_sql(con, sql))
            passed = actual == expected[e["id"]]
        except Exception as ex:
            error = str(ex)[:200]
            model_used = model_used or agent.LAST_MODEL_USED

        results.append({
            "id": e["id"], "difficulty": e["difficulty"], "passed": passed,
            "status": status, "repairs": repairs,
            "model_used": model_used, "sql": sql, "error": error,
        })
        extra = f" status={status} repairs={repairs}" if loop else ""
        print(f"[run {run_no}/{total_runs}] Q{e['id']:>2} [{e['difficulty']:<6}] "
              f"{'PASS' if passed else 'FAIL'}  ({model_used}){extra}"
              + (f"  ERR: {error}" if error else ""))
        time.sleep(4)  # stay under free-tier rate limits
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="baseline")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--loop", action="store_true",
                    help="evaluate the full agent loop (validate + repair) instead of one-shot SQL")
    args = ap.parse_args()

    con = get_con()
    schema = build_schema(con)
    evals = json.load(open("evals.json"))
    expected = {e["id"]: normalize(run_sql(con, e["gold_sql"])) for e in evals}

    all_runs = [run_once(con, schema, evals, expected, r + 1, args.runs, args.loop)
                for r in range(args.runs)]

    print(f"\nMode: {'agent loop' if args.loop else 'one-shot'}")
    print(f"Primary model:  {agent.MODEL}")
    print(f"Fallback model: {agent.FALLBACK_MODEL}")

    scores = [sum(x["passed"] for x in run) for run in all_runs]
    n = len(evals)
    print(f"\nScores per run: {scores} out of {n}")
    print(f"Mean accuracy: {sum(scores) / len(scores):.1f}/{n} "
          f"({100 * sum(scores) / (len(scores) * n):.0f}%)")

    for d in ["easy", "medium", "hard"]:
        ids = [e["id"] for e in evals if e["difficulty"] == d]
        got = sum(x["passed"] for run in all_runs for x in run if x["id"] in ids)
        print(f"  {d}: {got / len(all_runs):.1f}/{len(ids)}")

    if args.loop:
        repaired = sum(1 for run in all_runs for x in run if x["repairs"])
        print(f"Questions that needed a repair (across all runs): {repaired}")

    print("\nQuestions that did not pass every run:")
    unstable = False
    for e in evals:
        wins = sum(next(x for x in run if x["id"] == e["id"])["passed"] for run in all_runs)
        if wins < len(all_runs):
            unstable = True
            print(f"  Q{e['id']:>2} [{e['difficulty']}]: passed {wins}/{len(all_runs)}")
    if not unstable:
        print("  (none)")

    used = {}
    for run in all_runs:
        for x in run:
            used[x["model_used"]] = used.get(x["model_used"], 0) + 1
    print("Models used:", used)

    out = {
        "mode": "loop" if args.loop else "one-shot",
        "primary": agent.MODEL, "fallback": agent.FALLBACK_MODEL,
        "scores_per_run": scores,
        "results": all_runs[-1],   # last run, so inspect_fail.py keeps working
        "all_runs": all_runs,
    }
    with open(f"results_{args.tag}.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"Saved results_{args.tag}.json")


if __name__ == "__main__":
    main()