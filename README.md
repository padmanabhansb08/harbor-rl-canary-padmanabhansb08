# Harbor RL Submission — padmanabhansb08 (@padmanabhansb08)

## 1. Loom Walkthrough Video
- **Video URL:** [Watch 5-Minute Walkthrough](https://youtu.be/-8sxs2Q8jrs)
  *(Make sure link sharing is set to "Anyone with the link can view")*

## 2. Task & Calibration Summary

`tasks/reconcile-bug/` asks the agent to fix `/app/reconcile.py`, a CSV-to-JSON payment reconciler
(`total_valid_amount`, `valid_count`, `failed_count`).

- **Bug 1 (Easy):** the parser crashes with `ValueError` on blank or whitespace-only lines.
- **Bug 2 (Medium):** every non-`FAILED` row counts as valid, so `PENDING` transactions inflate the total and the valid count.
- **Bug 3 (Hard):** real-world CSV data is mishandled: duplicate `tx_id`s are resolved by comparing timestamp *strings* (wrong across UTC offsets), quoted amounts like `"1,250.00"` break the naive `split(",")` parser, and malformed rows (`NaN`/`1e3`/negative/3-decimal amounts, timestamps without an offset, unknown statuses, wrong field counts) are not skipped.
- **Calibration Scores:** `nop` = `0.00` | `oracle` (`solve.sh`) = `1.00` | Partial `oracle` (Bug 1+2 fixed, Bug 3 broken, shown in video) = `0.50` | `hack.sh` = `0.00` (hardened verifier; it scored `1.00` on the naive one)

**How the task was calibrated** (all jobs are listed in [`jobs_summary/INDEX.md`](jobs_summary/INDEX.md)):

| Attempt | Model | Bug 3 at the time | Result |
|---|---|---|---|
| `09-52-50` | `gemini-2.5-flash` | timezone dedup only | invalid: free tier is 20 requests/day/model, all 3 trials hit `RateLimitError` |
| `10-11-38` | `gemini-2.5-flash-lite` | timezone dedup only | invalid: model retired for new users (HTTP 404) |
| `10-15-00` | `gemini-3.5-flash-lite` | timezone dedup only | 3/3 pass: zero-signal trap |
| `10-22-15` | `gemini-3.5-flash-lite` | + quoted amounts with thousands separators | 3/3 pass: still zero-signal |
| `10-30-43` | `gemini-3.5-flash-lite` | + malformed-rows rule | **1/3 pass: used as Run 1** |

All later runs use `gemini/gemini-3.5-flash-lite` with `terminus-2`, `-k 3 -n 1`.

## 3. Ablation & RL Advantage Table (`terminus-2`, `k=3` Trials)

Dense weights: Bug 1 = 0.20, Bug 2 = 0.30, Bug 3 = 0.50. Runs 1 and 2 were graded by the binary
verifier; their dense scores are re-computed from each trial's saved `verifier/checks.json`.

| Run | Change Tested | Trial 1 (Binary / Dense) | Trial 2 (Binary / Dense) | Trial 3 (Binary / Dense) | Mean Binary | Mean Dense ($\bar{R}$) | Binary Advantage ($R_1-\bar{R}, R_2-\bar{R}, R_3-\bar{R}$) | Dense Advantage ($R_1-\bar{R}, R_2-\bar{R}, R_3-\bar{R}$) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Run 1: Baseline** | Original 3-bug task | `1 / 1.00` | `0 / 0.50` | `0 / 0.50` | `0.333` | `0.667` | `+0.667, -0.333, -0.333` | `+0.333, -0.167, -0.167` |
| **Run 2: Ablation** | Added `/app/sample.csv` (realistic messy input, no expected output) | `1 / 1.00` | `0 / 0.50` | `1 / 1.00` | `0.667` | `0.833` | `+0.333, -0.667, +0.333` | `+0.167, -0.333, +0.167` |

Trials: Run 1 = `H5bFDh7`, `RknJ4eM`, `fVv2Yw2` (job `2026-11-16__10-30-43`);
Run 2 = `73tBkzY`, `i3HwgaH`, `oTNn6DP` (job `2026-11-16__10-36-52`).

Facts from the logs:
- Both Run 1 failures (7 and 8 steps) crashed in the hidden malformed-rows case with
  `decimal.InvalidOperation`: their amount check compared `Decimal("NaN") < 0`, which raises.
  The passing trial took 17 steps.
- In Run 2, the two trials that ran their script on `/app/sample.csv` (25 mentions each in their
  trajectories) passed; `i3HwgaH` never opened it and failed with the same `NaN` crash.
- Every failed trial in both runs fixed Bugs 1 and 2, so all failures score 0.50 dense.

## 4. Trajectory Forensics (`trajectory.json`)
- **Where the Agent Failed in Run 1:** In trial `fVv2Yw2`, step 4 (`cat << 'EOF' > /app/reconcile.py`)
  rewrote the whole script from the spec, including a `parse_amount()` that wraps `Decimal(s)` in a
  `try/except InvalidOperation` but then runs `if d < 0 or d.as_tuple().exponent < -2` outside it.
  Its only test (step 5) was the 3-row example copied from `instruction.md`, which has no blank lines,
  PENDING rows, duplicates or malformed amounts; at step 6 it ran `rm /app/test.csv` and marked the task
  complete. In the hidden malformed-rows case `Decimal("nan")` parses without error, so `d < 0` raised
  `decimal.InvalidOperation` and the script crashed (Bug 3 failed; Bugs 1 and 2 passed).
- **Ablation Hypothesis & Result:** The failures were not a misunderstanding of the rules but a gap in
  the agents' own testing: both failed trials stopped after 7–8 steps without ever feeding a
  non-numeric amount through their code (`fVv2Yw2` tested only the clean spec example; `RknJ4eM` tested an
  invalid timestamp and `-5.00`, but never `NaN`). So the single change was to ship `/app/sample.csv`
  (messy real-world rows: `nan`, `5.5e1`, a naive timestamp, `CHARGEBACK`, a quoted thousands amount, a
  cross-offset duplicate) with no expected output. The pass rate went from 1/3 to 2/3: both trials that
  ran their script on the sample (`73tBkzY` at step 3, `oTNn6DP` at step 4) passed, and `oTNn6DP` visibly
  hit `decimal.InvalidOperation` on it at step 10 and fixed it before finishing, while `i3HwgaH` never
  opened the file and failed with the same `InvalidOperation` crash as the baseline. With only 3 trials per run this is evidence, not proof; the
  within-run link between using the sample and passing is the stronger signal.

## 5. Dense Reward & Anti-Cheat Design
- **My Dense Reward Weights (`tests/test.sh`):** Bug 1 = 0.20, Bug 2 = 0.30, Bug 3 = 0.50, each an
  independent pass/fail group of hidden cases, so a trial that fixes the easy bugs and stops scores 0.50,
  one that breaks the file scores 0.00, and a full fix scores 1.00. Half the reward sits on the hard bug
  so that "collect the easy points and stop" is a clearly worse policy than attempting Bug 3, even though
  touching parsing code risks dropping to 0.00. In these 6 trials every failure scored exactly 0.50, so the
  dense advantages have the same signs as the binary ones at half the size; dense grading would add
  information only when failures differ (e.g. 0.00 vs 0.50). Caveat: under the malformed-rows rule a blank
  line is itself a malformed row, so a correct Bug 3 fix also earns Bug 1's 0.20.
- **How `solution/hack.sh` Cheated & How I Blocked It:** `hack.sh` throws away the reconciler and
  writes a lookup table: it reads only the first `tx_id` of the input and prints a memorised answer for
  each of the 8 fixed hidden CSVs, which scored 1.00 on the naive verifier (job `10-47-10`, commit
  `25c2ace`). The hardened verifier (commit `e522e6d`) additionally generates 8 fresh CSVs per run from an
  `os.urandom` seed (logged in `checks.json`), with random IDs, amounts, offsets and file names, computes
  the expected output with a reference implementation, and runs the script with `python3 -I`; every
  random case keeps to its own bug group so the checks stay independent. `hack.sh` now scores 0.00 (job
  `10-51-21`) while `solve.sh` still scores 1.00.
- **Remaining Reward-Hacking Risk:** The agent runs as root in the same container the verifier later
  uses, so it could patch the `python3` binary or stdlib modules such as `json` or `decimal`, which the
  verifier's own reference implementation also imports, and make wrong answers compare as correct.
  Hash-checking the interpreter and stdlib, or running the reference checker from a separate trusted
  image, would close this.

## 6. AI Tool Workflow
- **AI Tool & Model Used:** Claude Code (desktop app, Claude Opus 5.5) for setup, the task code, the
  verifier, the trajectory analysis and drafting this README; a separate fresh sub-agent drafted
  `instruction.md`. Agent under test: `terminus-2` with `gemini/gemini-3.5-flash-lite`. The full
  sessions are in `ai_transcripts/`.
- **How I Avoided Prompt Leakage:** `instruction.md` was written by a separate sub-agent that was
  given only the business rules (input format, output keys, status, duplicate and later malformed-row
  rules) and never saw `reconcile.py`, `solve.sh` or the bug list; it was told not to name functions,
  line numbers or suspected bugs, and its example deliberately avoids blank lines, PENDING rows,
  duplicates and mixed offsets. The two rules added during calibration (quoted amounts, malformed rows)
  state required behaviour only and say nothing about what the existing script does wrong.
