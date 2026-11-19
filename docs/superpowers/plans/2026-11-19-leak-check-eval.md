# Leak check evaluation — 2026-11-19

`uv run python scripts/eval_leak_check.py` against `~typesafe/jev-latest` (OpenRouter Decisions API), with the fictional fixture in `tests/fixtures/leak_eval/` (40 cases: 20 leaking, 20 clean, including hard cases).

| | Lowest score | Highest score |
|---|---|---|
| Leaking messages | 1.42 | 2.58 |
| Clean messages | 0.05 | 1.00 |

- The two groups don't overlap. At any threshold between 1.00 and 1.42, precision and recall are both 1.0.
- The plan's default of 1.5 would miss one leak (a leak alluded to sideways, scored 1.42).
- **Chosen threshold: 1.2**, the midpoint of the gap. The check ships switched on.
- Hard cases that were caught: a keypad code spelled out in words (2.15), an alias inside a joke (1.95), a bare four-digit number (1.96).
- Hardest clean cases: an unrelated message on the same theme as a secret (1.00, "something with a lullaby vibe…"), and a savory-pie question near a secret recipe (0.97).

Re-run the evaluation when the Jev model version changes (`~typesafe/jev-latest` resolves to a dated model).
