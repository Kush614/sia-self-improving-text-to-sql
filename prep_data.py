#!/usr/bin/env python
"""Deterministically build the text-to-SQL task data from real Spider.

Produces (under tasks/text-to-sql/data/):

  public/test_questions.jsonl   {id, db_id, question}          # agent input (NO gold)
  public/train.jsonl            {id, db_id, question, query}   # few-shot pool (same DBs)
  public/schemas.json           {db_id: "CREATE TABLE ... ;"}  # convenience DDL
  public/databases/<db>/<db>.sqlite                            # copied SQLite DBs
  private/test_gold.jsonl       {id, query}                    # gold — agent NEVER sees this

Design notes (see INTERFACE_NOTES.md):
- Spider's train/dev databases are disjoint, so to give the agent a legitimate
  *same-DB, different-question* few-shot pool we draw both the scored set and the
  train pool from the chosen dev databases and split per-DB.
- Every emitted gold query is executed read-only and must succeed, and we PREFER
  questions whose gold returns a non-empty result set (so a wrong query that
  returns nothing can't accidentally "match" an empty gold).
- Fully deterministic (fixed DB list + seed) so re-running reproduces byte-identical
  splits.

Usage:
  python prep_data.py [--spider-dir DIR] [--out-dir DIR]
                      [--test-per-db 15] [--train-per-db 25]
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

SEED = 42

# Eight dev databases with rich multi-table schemas and enough questions to split.
CHOSEN_DBS = [
    "world_1",
    "car_1",
    "dog_kennels",
    "flight_2",
    "student_transcripts_tracking",
    "wta_1",
    "concert_singer",
    "pets_1",
]

DEFAULT_SPIDER_DIR = Path(__file__).resolve().parent / "downloads" / "spider_extracted" / "spider"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "tasks" / "text-to-sql" / "data"


def db_path(spider_dir: Path, db_id: str) -> Path:
    return spider_dir / "database" / db_id / f"{db_id}.sqlite"


def gold_runs_nonempty(spider_dir: Path, db_id: str, query: str) -> tuple[bool, bool]:
    """Return (executes_ok, nonempty). Read-only, short timeout."""
    p = db_path(spider_dir, db_id)
    if not p.exists():
        return False, False
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        cur = con.execute(query)
        rows = cur.fetchmany(1)
        con.close()
        return True, len(rows) > 0
    except sqlite3.Error:
        return False, False


def extract_schema(p: Path) -> str:
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    con.close()
    return ";\n\n".join(r[0].strip() for r in rows) + ";"


def write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spider-dir", type=Path, default=DEFAULT_SPIDER_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--test-per-db", type=int, default=15)
    ap.add_argument("--train-per-db", type=int, default=25)
    args = ap.parse_args()

    spider_dir: Path = args.spider_dir
    out_dir: Path = args.out_dir
    dev_path = spider_dir / "dev.json"
    if not dev_path.exists():
        raise SystemExit(f"dev.json not found at {dev_path}. Run the Spider download first (see README_RUN.md).")

    dev = json.loads(dev_path.read_text(encoding="utf-8"))

    # Group chosen-DB questions, deterministic order.
    by_db: dict[str, list[dict]] = defaultdict(list)
    for rec in dev:
        if rec["db_id"] in CHOSEN_DBS:
            by_db[rec["db_id"]].append({"db_id": rec["db_id"], "question": rec["question"], "query": rec["query"]})

    rng = random.Random(SEED)
    test_records: list[dict] = []
    gold_records: list[dict] = []
    train_records: list[dict] = []
    dropped = 0

    for db_id in CHOSEN_DBS:
        items = sorted(by_db.get(db_id, []), key=lambda r: (r["question"], r["query"]))
        if not items:
            print(f"  ! no questions for {db_id}")
            continue

        # Keep only gold that executes; sort non-empty first to avoid empty-match traps.
        good, empty = [], []
        for it in items:
            ok, nonempty = gold_runs_nonempty(spider_dir, db_id, it["query"])
            if not ok:
                dropped += 1
                continue
            (good if nonempty else empty).append(it)
        rng.shuffle(good)
        rng.shuffle(empty)
        pool = good + empty  # prefer non-empty for the scored slice

        n_test = args.test_per_db
        n_train = args.train_per_db
        if len(pool) < n_test + n_train:
            # Shrink the train slice first, then the test slice, to fit small DBs.
            n_train = max(0, len(pool) - n_test)
            if len(pool) < n_test:
                n_test = len(pool)
                n_train = 0
        test_slice = pool[:n_test]
        train_slice = pool[n_test:n_test + n_train]

        for i, it in enumerate(test_slice):
            rid = f"{db_id}__t{i:02d}"
            test_records.append({"id": rid, "db_id": db_id, "question": it["question"]})
            gold_records.append({"id": rid, "query": it["query"]})
        for i, it in enumerate(train_slice):
            rid = f"{db_id}__r{i:02d}"
            train_records.append({"id": rid, "db_id": db_id, "question": it["question"], "query": it["query"]})

        print(f"  {db_id:32s} pool={len(pool):3d} (nonempty={len(good)})  test={len(test_slice)}  train={len(train_slice)}")

    # ── Write output tree (clean rebuild) ────────────────────────────────────
    public = out_dir / "public"
    private = out_dir / "private"
    db_out = public / "databases"
    for d in (public, private):
        d.mkdir(parents=True, exist_ok=True)
    if db_out.exists():
        shutil.rmtree(db_out)
    db_out.mkdir(parents=True)

    write_jsonl(public / "test_questions.jsonl", test_records)
    write_jsonl(public / "train.jsonl", train_records)
    write_jsonl(private / "test_gold.jsonl", gold_records)

    schemas: dict[str, str] = {}
    for db_id in CHOSEN_DBS:
        src = db_path(spider_dir, db_id)
        if not src.exists():
            continue
        dest_dir = db_out / db_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / f"{db_id}.sqlite")
        schemas[db_id] = extract_schema(src)
    (public / "schemas.json").write_text(json.dumps(schemas, indent=2), encoding="utf-8")

    # ── Final assertion: 100% of emitted gold executes ───────────────────────
    gold_by_id = {g["id"]: g["query"] for g in gold_records}
    qmeta = {t["id"]: t for t in test_records}
    failures = 0
    # Re-check against the COPIED dbs to be sure the deployed artifacts work.
    for rid, q in gold_by_id.items():
        db_id = qmeta[rid]["db_id"]
        p = db_out / db_id / f"{db_id}.sqlite"
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            con.execute(q).fetchmany(1)
            con.close()
        except sqlite3.Error as e:
            failures += 1
            print(f"  !! gold failed on copied db: {rid}: {e}")

    print("\n=== SUMMARY ===")
    print(f"  databases : {len(schemas)}")
    print(f"  test (scored) : {len(test_records)}")
    print(f"  train (few-shot pool) : {len(train_records)}")
    print(f"  gold dropped (did not execute) : {dropped}")
    print(f"  gold re-check failures on copied DBs : {failures}")
    print(f"  output : {out_dir}")
    if failures:
        raise SystemExit("ERROR: some gold queries failed on the copied DBs.")
    print("  OK: 100% of emitted gold queries execute.")


if __name__ == "__main__":
    main()
