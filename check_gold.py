import json, duckdb

con = duckdb.connect()
con.sql("INSTALL sqlite; LOAD sqlite;")
con.sql("ATTACH 'Chinook_Sqlite.sqlite' AS chinook (TYPE sqlite);")
con.sql("USE chinook;")

for e in json.load(open("evals.json")):
    print(f"\n--- Q{e['id']} ({e['difficulty']}): {e['question']}")
    df = con.sql(e["gold_sql"]).df()
    print(f"rows: {len(df)}")
    print(df.head(8))
