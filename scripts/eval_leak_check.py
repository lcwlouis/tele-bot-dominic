"""Score fictional outbound messages with Jev.

Run with `uv run python scripts/eval_leak_check.py` and OPENROUTER_API_KEY set in
the environment. The evaluation sends the synthetic keywords and cases to OpenRouter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from dom.guard import leak_score, private_terms
from dom.home import Home
from dom.jev import JevClient

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "leak_eval"
TARGET = 0.9


@dataclass(frozen=True)
class Case:
    text: str
    leak: bool


def parse_cases(text: str) -> list[Case]:
    cases = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"line {line_no}: invalid JSON") from exc
        if (
            not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip()
            or type(item.get("leak")) is not bool
        ):
            raise ValueError(f"line {line_no}: expected non-empty text and a boolean leak label")
        cases.append(Case(item["text"], item["leak"]))
    if not cases:
        raise ValueError("no evaluation cases")
    return cases


def metrics(results: list[tuple[Case, float | None]], threshold: float) -> tuple[float, float]:
    held = [case for case, score in results if score is not None and score >= threshold]
    true_positives = sum(case.leak for case in held)
    positives = sum(case.leak for case, _ in results)
    return (true_positives / len(held) if held else 0.0, true_positives / positives if positives else 0.0)


def best_threshold(results: list[tuple[Case, float | None]]) -> tuple[float, float, float]:
    thresholds = {score for _, score in results if score is not None} or {1.5}

    def rank(candidate: tuple[float, float, float]) -> tuple[bool, float, float, float]:
        threshold, precision, recall = candidate
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return precision >= TARGET and recall >= TARGET, f1, recall, threshold

    return max(((threshold, *metrics(results, threshold)) for threshold in thresholds), key=rank)


async def evaluate(home: Home, cases: list[Case], key: str, model: str) -> int:
    terms = private_terms(home)
    results = []
    async with httpx.AsyncClient() as http:
        jev = JevClient(http, key, model)
        for number, case in enumerate(cases, start=1):
            score = await leak_score(jev, terms, case.text)
            results.append((case, score))
            print(f"{number:02d} {'leak ' if case.leak else 'clean'} {score if score is not None else 'unavailable'}  {case.text}")
    threshold, precision, recall = best_threshold(results)
    print(f"Best threshold {threshold:g} | precision {precision:.3f} | recall {recall:.3f}")
    return 0 if precision >= TARGET and recall >= TARGET else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="~typesafe/jev-latest")
    args = parser.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        parser.error("set OPENROUTER_API_KEY in the environment")
    home = Home(FIXTURES)
    try:
        cases = parse_cases(home.read("cases.jsonl"))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return asyncio.run(evaluate(home, cases, key, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
