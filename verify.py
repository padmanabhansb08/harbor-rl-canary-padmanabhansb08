#!/usr/bin/env python3
"""Verifier for the reconcile-bug task.

Each bug has its own group of hidden CSV cases, built so that a group only
exercises its own bug (no blank lines outside group 1, no PENDING rows
outside group 2, no duplicate tx_ids or quoted fields outside group 3). That keeps the three
checks independent, which a partial-credit reward needs.

Expected outputs come from reference() below, not from hard-coded numbers.

Anti-cheat (hardened version): on top of the fixed cases, every run generates fresh
randomized CSVs per bug group from an os.urandom seed, with random file names. A script
that memorises answers for known inputs (solution/hack.sh) cannot pass them.
"""
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

APP = Path("/app/reconcile.py")
CASES = Path(__file__).resolve().parent / "cases"
LOG_DIR = Path("/logs/verifier")

CHECKS = {
    "bug1_blank_lines": ["blank_lines_a.csv", "blank_lines_b.csv"],
    "bug2_status_filter": ["pending_a.csv", "pending_b.csv"],
    "bug3_dedup_timezones": ["dedup_tz_a.csv", "dedup_tz_b.csv", "quoted_amounts.csv", "malformed_rows.csv"],
}

# Dense reward: weighted partial credit per bug. Most weight on the hard bug so that
# "fix the easy bugs and stop" caps the score at 0.50 (see README, local-optimum answer).
WEIGHTS = {
    "bug1_blank_lines": 0.20,
    "bug2_status_filter": 0.30,
    "bug3_dedup_timezones": 0.50,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


AMOUNT = re.compile(r"^(\d+|\d{1,3}(,\d{3})+)(\.\d{1,2})?$")


def is_valid(fields):
    """Malformed-row rule from instruction.md."""
    if len(fields) != 4:
        return False
    _, ts, amount, status = (p.strip() for p in fields)
    if status.upper() not in {"SUCCESS", "FAILED", "PENDING"} or not AMOUNT.match(amount):
        return False
    try:
        return datetime.fromisoformat(ts).utcoffset() is not None
    except ValueError:
        return False


def reference(path):
    """Correct implementation of the business rules in instruction.md."""
    latest = {}
    with open(path, newline="", encoding="utf-8") as fh:
        records = list(csv.reader(fh))
    for fields in records[1:]:
        if not "".join(fields).strip() or not is_valid(fields):
            continue
        tx_id, ts, amount, status = (p.strip() for p in fields)
        when = datetime.fromisoformat(ts)
        if tx_id not in latest or when >= latest[tx_id][0]:
            latest[tx_id] = (when, Decimal(amount.replace(",", "")), status.upper())
    total, valid, failed = Decimal("0"), 0, 0
    for _, amount, status in latest.values():
        if status == "SUCCESS":
            valid += 1
            total += amount
        elif status == "FAILED":
            failed += 1
    return {
        "total_valid_amount": total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "valid_count": valid,
        "failed_count": failed,
    }


# ---------------------------------------------------------------------------
# Anti-cheat: fresh randomized cases, generated at verification time.
# A script that memorises the fixed cases above (solution/hack.sh) cannot know these.
# Each generator keeps to its own bug group, so the checks stay independent.
# ---------------------------------------------------------------------------
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts(instant, offset_min, rng):
    if offset_min == 0 and rng.random() < 0.5:
        return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return instant.astimezone(timezone(timedelta(minutes=offset_min))).isoformat()


def _instant(rng):
    return BASE + timedelta(minutes=rng.randint(0, 300 * 24 * 60))


def _amount(rng, lo=1, hi=999):
    return f"{Decimal(rng.randint(lo * 100, hi * 100)) / 100:.2f}"


def _ids(rng, n):
    ids = set()
    while len(ids) < n:
        ids.add(f"{rng.choice(['TX', 'PAY', 'INV', 'R'])}{rng.randint(10000, 999999)}")
    return list(ids)


def _status(rng, word):
    return rng.choice([word, word.lower(), word.title(), f" {word} "])


def gen_blank_lines(rng):
    """Bug 1 group: valid rows, blank / whitespace-only lines anywhere."""
    lines = [f"{t},{_ts(_instant(rng), rng.choice([0, 60, -300]), rng)},{_amount(rng)},"
             f"{_status(rng, rng.choice(['SUCCESS', 'FAILED']))}" for t in _ids(rng, rng.randint(5, 9))]
    lines[0] = lines[0].rsplit(",", 1)[0] + ",SUCCESS"
    for _ in range(rng.randint(2, 4)):
        lines.insert(rng.randint(0, len(lines)), rng.choice(["", "   ", "\t", " \t "]))
    return ["tx_id,timestamp,amount,status"] + lines


def gen_pending(rng):
    """Bug 2 group: PENDING rows mixed in, no blank lines, no duplicates."""
    statuses = ["SUCCESS", "PENDING", "PENDING"] + [rng.choice(["SUCCESS", "FAILED", "PENDING"])
                                                    for _ in range(rng.randint(3, 6))]
    rng.shuffle(statuses)
    ids = _ids(rng, len(statuses))
    return ["tx_id,timestamp,amount,status"] + [
        f"{t},{_ts(_instant(rng), rng.choice([0, 330, -240]), rng)},{_amount(rng)},{_status(rng, s)}"
        for t, s in zip(ids, statuses)]


def gen_dedup_tz(rng):
    """Bug 3 group: duplicates whose text order disagrees with their real time order."""
    rows = []
    ids = _ids(rng, rng.randint(5, 8))
    for t in ids[:rng.randint(3, 5)]:
        earlier = _instant(rng)
        later = earlier + timedelta(minutes=rng.randint(10, 240))
        s_early, s_late = rng.choice([("SUCCESS", "FAILED"), ("FAILED", "SUCCESS")])
        amount = _amount(rng)
        # Large positive offset on the earlier row, large negative on the later row:
        # the earlier row's local clock text sorts AFTER the later row's.
        rows.append(f"{t},{_ts(earlier, rng.choice([300, 330, 420, 480, 540, 600]), rng)},{amount},{s_early}")
        rows.append(f"{t},{_ts(later, -rng.choice([180, 240, 300, 360, 420, 480]), rng)},{amount},{s_late}")
    for t in ids[len(rows) // 2:]:
        rows.append(f"{t},{_ts(_instant(rng), 0, rng)},{_amount(rng)},{rng.choice(['SUCCESS', 'FAILED'])}")
    rng.shuffle(rows)
    return ["tx_id,timestamp,amount,status"] + rows


def gen_quoted(rng):
    """Bug 3 group: quoted fields, thousands separators, a comma inside a quoted tx_id."""
    rows = []
    ids = _ids(rng, rng.randint(4, 7))
    ids[0] = f'"{ids[0][:3]},{ids[0][3:]}"'
    for i, t in enumerate(ids):
        big = Decimal(rng.randint(1000_00, 9_999_999_99)) / 100
        amount = f'"{big:,.2f}"' if i % 2 == 0 else _amount(rng)
        ts = _ts(_instant(rng), rng.choice([0, 60, -120]), rng)
        if rng.random() < 0.3:
            ts = f'"{ts}"'
        if rng.random() < 0.3 and not t.startswith('"'):
            t = f'"{t}"'
        rows.append(f"{t},{ts},{amount},{rng.choice(['SUCCESS', 'FAILED'])}")
    rows[0] = rows[0].rsplit(",", 1)[0] + ",SUCCESS"
    return ["tx_id,timestamp,amount,status"] + rows


def gen_malformed(rng):
    """Bug 3 group: malformed rows the stdlib parsers would happily accept, plus a malformed
    later duplicate that must NOT replace an earlier valid row."""
    ids = _ids(rng, 10)
    rows, valid_ids = [], ids[:rng.randint(3, 5)]
    stamps = {}
    for t in valid_ids:
        stamps[t] = _instant(rng)
        rows.append(f"{t},{_ts(stamps[t], 0, rng)},{_amount(rng)},{rng.choice(['SUCCESS', 'FAILED'])}")
    dup = valid_ids[0]
    naive_later = (stamps[dup] + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    rows.append(f"{dup},{naive_later},{_amount(rng)},{rng.choice(['SUCCESS', 'FAILED'])}")
    ok_ts = lambda: _ts(_instant(rng), 0, rng)
    bad = [
        lambda t: f"{t},{ok_ts()},{rng.choice(['nan', 'NaN', 'Infinity', 'sNaN'])},SUCCESS",
        lambda t: f"{t},{ok_ts()},{rng.choice(['1e3', '2.5E2', '7e1'])},SUCCESS",
        lambda t: f"{t},{ok_ts()},-{_amount(rng)},{rng.choice(['SUCCESS', 'FAILED'])}",
        lambda t: f"{t},{ok_ts()},{rng.randint(1, 99)}.{rng.randint(100, 999)},SUCCESS",
        lambda t: f"{t},{ok_ts()},{_amount(rng)},{rng.choice(['REFUNDED', 'CHARGEBACK', 'VOID'])}",
        lambda t: f"{t},{ok_ts()},{_amount(rng)},SUCCESS,{rng.choice(['extra', 'x', '1'])}",
        lambda t: f"{t},{ok_ts()},{_amount(rng)}",
        lambda t: f"{t},{(_instant(rng)).replace(tzinfo=None).isoformat()},{_amount(rng)},FAILED",
        lambda t: f"{t},{rng.choice(['not-a-date', '2026-13-01T00:00:00+00:00', 'yesterday'])},{_amount(rng)},SUCCESS",
    ]
    rng.shuffle(bad)
    for t, make in zip(ids[5:], bad[:5]):
        rows.append(make(t))
    first, rest = rows[0], rows[1:]
    rng.shuffle(rest)
    return ["tx_id,timestamp,amount,status", first] + rest


RANDOM = {
    "bug1_blank_lines": [gen_blank_lines, gen_blank_lines],
    "bug2_status_filter": [gen_pending, gen_pending],
    "bug3_dedup_timezones": [gen_dedup_tz, gen_dedup_tz, gen_quoted, gen_malformed],
}


def run_case(case):
    """Run the agent's script on one case. Returns (passed, detail)."""
    expected = reference(case)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / f"{uuid.uuid4().hex}.csv"
        shutil.copy(case, csv_path)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(APP), str(csv_path)],  # -I: ignore PYTHON* env vars and user site
                cwd=tmp, capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            return False, "timeout"
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {proc.stderr.strip()[-300:]}"
    try:
        got = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"stdout is not JSON: {proc.stdout[-300:]!r}"
    if not isinstance(got, dict):
        return False, f"JSON is not an object: {got!r}"
    for key in ("valid_count", "failed_count"):
        if type(got.get(key)) is not int or got[key] != expected[key]:
            return False, f"{key}: got {got.get(key)!r}, want {expected[key]}"
    amount = got.get("total_valid_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return False, f"total_valid_amount is not a number: {amount!r}"
    if abs(Decimal(str(amount)) - expected["total_valid_amount"]) > Decimal("0.001"):
        return False, f"total_valid_amount: got {amount}, want {expected['total_valid_amount']}"
    return True, "ok"


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    seed = int.from_bytes(os.urandom(8), "big")
    rng = random.Random(seed)
    gen_dir = Path(tempfile.mkdtemp(prefix="cases-"))
    results, details = {}, {}
    for name, files in CHECKS.items():
        cases = {f: CASES / f for f in files}
        for i, gen in enumerate(RANDOM[name]):
            path = gen_dir / f"{uuid.uuid4().hex}.csv"
            path.write_text("\n".join(gen(rng)) + "\n", encoding="utf-8")
            cases[f"random:{gen.__name__}#{i}"] = path
        outcomes = {f: run_case(p) for f, p in cases.items()} if APP.exists() else {
            f: (False, "missing /app/reconcile.py") for f in cases}
        results[name] = all(ok for ok, _ in outcomes.values())
        details[name] = {f: msg for f, (_, msg) in outcomes.items()}
        print(f"[{'PASS' if results[name] else 'FAIL'}] {name}")
        for f, msg in details[name].items():
            print(f"    {f}: {msg}")

    binary = 1.0 if all(results.values()) else 0.0
    dense = round(sum(WEIGHTS[name] for name, ok in results.items() if ok), 2)
    print(f"binary={binary:.2f} dense={dense:.2f}")
    (LOG_DIR / "checks.json").write_text(json.dumps(
        {"checks": results, "details": details, "weights": WEIGHTS, "random_seed": seed,
         "binary_reward": binary, "dense_reward": dense}, indent=2))
    shutil.rmtree(gen_dir, ignore_errors=True)
    (LOG_DIR / "reward.txt").write_text(f"{dense:.2f}\n")


if __name__ == "__main__":
    main()
