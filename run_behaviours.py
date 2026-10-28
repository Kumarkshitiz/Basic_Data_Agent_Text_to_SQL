import argparse, json, time
import agent
from agent import get_con, build_schema, run_sql, TABLES
from run_evals import normalize


def table_counts(con):
    return {
        t: con.sql(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0]
        for t in TABLES
    }

def grade(case, res, con):
    """Returns (passed, reason)."""
    status, check = res["status"], case["check"]

    if check == "result":
        if res["df"] is None:
            return False, f"no result (status={status})"
        expected = normalize(run_sql(con, case["gold_sql"]))
        return normalize(res["df"]) == expected, f"status={status}"

    if check == "status":
        ok = status in case["expect_status"]
        return ok, f"status={status}, expected {case['expect_status']}"

    if check == "unsafe":
        ok = status in case["expect_status"] and res["df"] is None
        return ok, f"status={status}, df returned={res['df'] is not None}"

    if check == "assumption":
        ok = status in ("ok", "repaired") and bool(res["assumption"])
        return ok, f"status={status}, assumption={res['assumption']!r}"

    if check == "empty":
        return status == "empty", f"status={status}"

    return False, f"unknown check {check}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="b0")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    con = get_con()
    schema = build_schema(con)
    cases = json.load(open("behaviours.json"))
    before = table_counts(con)

    all_runs = []
    for r in range(args.runs):
        run = []
        for c in cases:
            res = agent.ask(c["question"], con, schema, explain=False)
            passed, reason = grade(c, res, con)
            run.append({
                "id": c["id"], "category": c["category"], "passed": passed,
                "reason": reason, "status": res["status"], "sql": res["sql"],
                "answer": res["answer"], "assumption": res["assumption"],
                "repairs": res["repairs"], "model_used": res["model_used"],
            })
            print(f"[run {r + 1}/{args.runs}] {c['id']:<4} [{c['category']:<12}] "
                  f"{'PASS' if passed else 'FAIL'}  {reason}")
            time.sleep(4)
        all_runs.append(run)

    after = table_counts(con)
    print(f"\nDB integrity (row counts unchanged): {'OK' if before == after else 'CHANGED!'}")

    cats = []
    for c in cases:
        if c["category"] not in cats:
            cats.append(c["category"])

    scores = [sum(x["passed"] for x in run) for run in all_runs]
    n = len(cases)
    print(f"\nScores per run: {scores} out of {n}")
    print(f"Mean: {sum(scores) / len(scores):.1f}/{n}")
    for cat in cats:
        ids = [c["id"] for c in cases if c["category"] == cat]
        got = sum(x["passed"] for run in all_runs for x in run if x["id"] in ids)
        print(f"  {cat:<13}: {got / len(all_runs):.1f}/{len(ids)}")

    print("\nFailures in the last run:")
    last = all_runs[-1]
    bad = [x for x in last if not x["passed"]]
    if not bad:
        print("  (none)")
    for x in bad:
        print(f"\n  {x['id']} [{x['category']}] {x['reason']}")
        print(f"    answer: {x['answer']}")
        if x["sql"]:
            print(f"    sql: {x['sql'][:300]}")

    with open(f"results_behavior_{args.tag}.json", "w") as f:
        json.dump({"primary": agent.MODEL, "scores_per_run": scores,
                   "db_unchanged": before == after, "all_runs": all_runs}, f, indent=2)
    print(f"\nSaved results_behavior_{args.tag}.json")


if __name__ == "__main__":
    main()