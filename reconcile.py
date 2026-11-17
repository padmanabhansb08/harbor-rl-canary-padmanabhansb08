#!/usr/bin/env python3
"""reconcile.py - summarise a CSV export of payment transactions.

Usage: python3 reconcile.py <transactions.csv>

Prints a JSON object with total_valid_amount, valid_count and failed_count.
"""
import json
import sys
from decimal import Decimal, ROUND_HALF_UP

HEADER = ["tx_id", "timestamp", "amount", "status"]


def read_rows(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    if not lines or [h.strip() for h in lines[0].split(",")] != HEADER:
        sys.exit(f"error: expected header {','.join(HEADER)}")
    rows = []
    for line in lines[1:]:
        tx_id, timestamp, amount, status = (p.strip() for p in line.split(","))
        rows.append({
            "tx_id": tx_id,
            "timestamp": timestamp,
            "amount": Decimal(amount),
            "status": status.upper(),
        })
    return rows


def latest_per_tx(rows):
    """Keep one row per tx_id: the most recent one."""
    latest = {}
    for row in rows:
        seen = latest.get(row["tx_id"])
        if seen is None or row["timestamp"] >= seen["timestamp"]:
            latest[row["tx_id"]] = row
    return list(latest.values())


def summarise(rows):
    total = Decimal("0")
    valid = failed = 0
    for row in rows:
        if row["status"] == "FAILED":
            failed += 1
            continue
        valid += 1
        total += row["amount"]
    return {
        "total_valid_amount": float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "valid_count": valid,
        "failed_count": failed,
    }


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: reconcile.py <transactions.csv>")
    rows = latest_per_tx(read_rows(argv[1]))
    print(json.dumps(summarise(rows)))


if __name__ == "__main__":
    main(sys.argv)
