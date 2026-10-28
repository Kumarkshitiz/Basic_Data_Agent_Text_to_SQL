import argparse, json, time
import agent
from agent import get_con, build_schema, run_sql
from run_evals import normalize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", default="m0")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    con = get_con()
    schema = build_schema(con)
    convos = json.load(open("multiturn.json"))
    expected = {c["id"]: normalize(run_sql(con, c["gold_sql"])) for c in convos}

    all_runs = []
    for r in range(args.runs):
        run = []
        for c in convos:
            history, res = [], None
            for q in c["turns"]:
                res = agent.ask(q, con, schema, explain=False, history=history)
                if res["status"] in ("ok", "repaired", "empty"):
                    history.append({"question": q, "sql": res["sql"]})
                time.sleep(4)
            passed = res["df"] is not None and normalize(res["df"]) == expected[c["id"]]
            run.append({"id": c["id"], "passed": passed, "status": res["status"],
                        "last_turn": c["turns"][-1], "standalone": res.get("standalone"),
                        "sql": res["sql"]})
            print(f"[run {r + 1}/{args.runs}] {c['id']} {'PASS' if passed else 'FAIL'} "
                  f"status={res['status']}  standalone={res.get('standalone')!r}")
        all_runs.append(run)

    scores = [sum(x["passed"] for x in run) for run in all_runs]
    print(f"\nScores per run: {scores} out of {len(convos)}")
    print("\nFailures in the last run:")
    bad = [x for x in all_runs[-1] if not x["passed"]]
    if not bad:
        print("  (none)")
    for x in bad:
        print(f"\n  {x['id']}: last turn = {x['last_turn']!r}")
        print(f"    rewritten as: {x['standalone']!r}")
        print(f"    sql: {(x['sql'] or '')[:300]}")

    with open(f"results_multiturn_{args.tag}.json", "w") as f:
        json.dump({"scores_per_run": scores, "all_runs": all_runs}, f, indent=2)
    print(f"\nSaved results_multiturn_{args.tag}.json")


if __name__ == "__main__":
    main()