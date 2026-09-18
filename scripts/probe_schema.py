"""Read-only diagnostic: newspaper_articles schema + duplicate analysis.

Run from the repository root so ``load_dotenv()`` can find ``.env``:

    python scripts/probe_schema.py

Performs no writes — safe to run against production.
"""
import os, sys
from collections import Counter
from dotenv import load_dotenv
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

print("--- newspaper_articles full column list ---")
r = sb.table("newspaper_articles").select("*").limit(1).execute()
print("  ", sorted((r.data or [{}])[0].keys()))

print()
print("--- row counts ---")
for t in ["newspaper_articles", "articles", "newspaper_numbers", "independent_articles"]:
    try:
        c = sb.table(t).select("*", count="exact").limit(1).execute()
        print(f"   {t:22} {c.count}")
    except Exception as e:
        print(f"   {t:22} ERR {str(e)[:80]}")

print()
print("--- duplicates in newspaper_articles ---")
rows = []
start = 0
while True:
    b = (sb.table("newspaper_articles")
         .select("id, headline, issue_id, newspaper_name, page_number, article_hash")
         .range(start, start + 999).execute()).data or []
    rows.extend(b)
    if len(b) < 1000:
        break
    start += 1000
    if start > 20000:
        break
print("  scanned:", len(rows))

for label, fn in [
    ("(newspaper,issue_id,headline)", lambda x: (x.get("newspaper_name"), x.get("issue_id"), x.get("headline"))),
    ("article_hash", lambda x: x.get("article_hash")),
    ("(newspaper,issue_id,headline,page)", lambda x: (x.get("newspaper_name"), x.get("issue_id"), x.get("headline"), x.get("page_number"))),
]:
    keys = Counter(fn(x) for x in rows)
    dup = {k: v for k, v in keys.items() if v > 1 and k not in (None, (None, None, None))}
    print(f"  {label:36} distinct={len(keys):5} dupkeys={len(dup):4} extra={sum(v-1 for v in dup.values()):5}")

print()
print("--- sample duplicate groups (headline truncated) ---")
keys = Counter((x.get("newspaper_name"), x.get("issue_id"), x.get("headline")) for x in rows)
shown = 0
for k, v in keys.most_common():
    if v > 1 and k[0] and k[2] and shown < 5:
        ids = [x["id"] for x in rows
               if (x.get("newspaper_name"), x.get("issue_id"), x.get("headline")) == k]
        print(f"   x{v}  ids={ids}")
        print(f"        {str(k[0])[:14]} | issue={k[1]} | {str(k[2])[:60]}")
        shown += 1

print()
print("--- issues table probe ---")
for t in ["issues", "issue", "newspaper_issues"]:
    try:
        rr = sb.table(t).select("*").limit(1).execute()
        print(f"   FOUND {t}: cols={sorted((rr.data or [{}])[0].keys())}")
    except Exception as e:
        m = str(e)
        print(f"   {'absent' if ('PGRST205' in m or '404' in m or 'Could not find' in m) else 'ERR'} {t}")