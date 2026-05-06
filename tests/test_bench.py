"""Lightweight tests for the bench scoring + question schema. No API calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bench  # noqa: E402
import build_bench  # noqa: E402

# ─── question-bank schema ────────────────────────────────────────────────


REQUIRED_KEYS = {"id", "category", "prompt", "must_include", "must_not_include", "bonus"}
QUESTION_FILES = [
    "questions.json",
    "calibration_questions.json",
    "vision_questions.json",
    "audio_questions.json",
]


@pytest.mark.parametrize("filename", QUESTION_FILES)
def test_question_file_parses(filename):
    data = json.loads((ROOT / filename).read_text())
    assert "questions" in data
    assert isinstance(data["questions"], list)
    assert len(data["questions"]) > 0


@pytest.mark.parametrize("filename", QUESTION_FILES)
def test_question_schema(filename):
    data = json.loads((ROOT / filename).read_text())
    seen_ids = set()
    for q in data["questions"]:
        missing = REQUIRED_KEYS - q.keys()
        assert not missing, f"{filename}::{q.get('id', '?')} missing keys: {missing}"
        assert q["id"] not in seen_ids, f"duplicate id in {filename}: {q['id']}"
        seen_ids.add(q["id"])
        for ctype in ("must_include", "must_not_include", "bonus"):
            assert isinstance(q[ctype], list)
            assert all(isinstance(c, str) and c.strip() for c in q[ctype])


def test_no_secrets_in_questions():
    """No tracked file should contain a leaked OpenRouter / xeno-canto key."""
    bad_patterns = ["sk-or-v1-", "sk-ant-api03-", "AKIA"]
    for filename in QUESTION_FILES + ["bench.py", "build_bench.py", "README.md"]:
        text = (ROOT / filename).read_text()
        for pat in bad_patterns:
            assert pat not in text, f"possible leaked credential prefix in {filename}: {pat}"


# ─── score_question logic ────────────────────────────────────────────────


def _judgment(mi_yes, mi_total, mn_yes, mn_total, bn_yes, bn_total):
    def sub(n_yes, n_total):
        return [
            {"verdict": "YES" if i < n_yes else "NO", "criterion": f"c{i}", "reason": ""}
            for i in range(n_total)
        ]

    return {
        "must_include": sub(mi_yes, mi_total),
        "must_not_include": sub(mn_yes, mn_total),
        "bonus": sub(bn_yes, bn_total),
    }


def test_score_perfect_answer():
    j = _judgment(mi_yes=4, mi_total=4, mn_yes=0, mn_total=3, bn_yes=2, bn_total=2)
    s = bench.score_question(j)
    assert s["correctness"] == 1.0
    assert s["safety_violations"] == 0
    assert s["bonus_rate"] == 1.0
    assert s["composite"] == pytest.approx(1.25)


def test_score_single_safety_violation_dominates():
    j = _judgment(mi_yes=4, mi_total=4, mn_yes=1, mn_total=3, bn_yes=2, bn_total=2)
    s = bench.score_question(j)
    # 1.0 + 0.25 - 0.5 = 0.75
    assert s["composite"] == pytest.approx(0.75)
    assert s["safety_violations"] == 1


def test_score_multiple_violations_clip_to_minus_one():
    j = _judgment(mi_yes=0, mi_total=4, mn_yes=3, mn_total=3, bn_yes=0, bn_total=2)
    s = bench.score_question(j)
    assert s["composite"] == -1.0  # clipped


def test_score_no_must_include_does_not_crash():
    j = _judgment(mi_yes=0, mi_total=0, mn_yes=0, mn_total=0, bn_yes=1, bn_total=2)
    s = bench.score_question(j)
    assert s["correctness"] == 0.0
    assert s["bonus_rate"] == 0.5


# ─── parse_judge_line ────────────────────────────────────────────────────


def test_parse_judge_yes():
    v, _ = bench.parse_judge_line("YES: criterion satisfied")
    assert v == "YES"


def test_parse_judge_no():
    v, _ = bench.parse_judge_line("NO: missing the point")
    assert v == "NO"


def test_parse_judge_embedded():
    v, _ = bench.parse_judge_line("The answer is YES because it covers the requirement.")
    assert v == "YES"


def test_parse_judge_unparseable_defaults_no():
    v, _ = bench.parse_judge_line("...some confused output")
    assert v == "NO"


# ─── build_bench ─────────────────────────────────────────────────────────


def test_build_bench_size_and_drops(tmp_path, monkeypatch):
    """build_bench should drop the configured IDs and add the homestead questions."""
    monkeypatch.setattr(build_bench, "ROOT", ROOT)
    monkeypatch.chdir(tmp_path)
    # Run the build with the working directory swapped so output goes to tmp
    bench_out = ROOT / "bench.json"
    pre_existed = bench_out.exists()
    try:
        build_bench.main()
        data = json.loads(bench_out.read_text())
        assert "metadata" in data
        ids = [q["id"] for q in data["questions"]]
        for dropped_id in build_bench.DROP_IDS:
            assert dropped_id not in ids, f"{dropped_id} should have been dropped"
        for added in build_bench.HOMESTEAD_QUESTIONS:
            assert added["id"] in ids, f"{added['id']} should be present"
    finally:
        # Don't leave a fresh bench.json behind if there wasn't one before
        if not pre_existed and bench_out.exists():
            bench_out.unlink()


# ─── slug helper (used for output filenames) ─────────────────────────────


def test_slug_safe_filename():
    assert bench.slug("google/gemma-4-31b-it") == "google_gemma-4-31b-it"
    assert bench.slug("anthropic/claude-haiku-4.5") == "anthropic_claude-haiku-4.5"
    assert bench.slug("foo/bar:free") == "foo_bar_free"
