#!/usr/bin/env python3
"""Generate audio-bench answers with the official NatureLM-audio Python API.

NatureLM-audio is a local PyTorch model rather than an OpenAI-compatible audio
endpoint, so this runner writes answers in `bench.py`'s standard format for the
existing `judge` and `report` commands.

The upstream `naturelm infer` CLI currently omits its required `merging_alpha`
argument on macOS. This script uses the supported Python API instead and can
therefore set that value explicitly.

The official processor truncates each inference window to 10 seconds. With the
default 10-second window and hop, clips shorter than 20 seconds are evaluated
from their first 10 seconds; a 20-second clip receives two windows.

Example:

    # In the NatureLM-audio environment:
    python scripts/run_naturelm_audio.py \\
        --questions audio_questions.json \\
        --out-dir results-audio-naturelm \\
        --device auto

    # Then, in this repository's normal environment:
    python bench.py judge --questions audio_questions.json \\
        --out-dir results-audio-naturelm --resume
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bench  # noqa: E402

MODEL_ID = "EarthSpeciesProject/NatureLM-audio"
_WINDOW_PREFIX = re.compile(r"^#\d+(?:\.\d+)?s - \d+(?:\.\d+)?s#:\s*")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", default="audio_questions.json", help="benchmark question JSON")
    parser.add_argument("--out-dir", default="results-audio-naturelm", help="answers output directory")
    parser.add_argument("--question-id", help="run one question only (smoke/debug)")
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cuda", "mps", "cpu"),
        help="PyTorch device; auto prefers CUDA, then Apple MPS, then CPU",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=10.0,
        help="NatureLM sliding-window length in seconds (official inference default)",
    )
    parser.add_argument(
        "--hop-seconds",
        type=float,
        default=10.0,
        help="NatureLM sliding-window hop in seconds (official inference default)",
    )
    parser.add_argument(
        "--merging-alpha",
        type=float,
        default=1.0,
        help="LoRA/base merge alpha; 1.0 is the published NatureLM checkpoint",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=4096,
        help="generation cap used in the benchmark's other audio runs",
    )
    return parser.parse_args()


def build_query(question: dict[str, Any]) -> str:
    """Combine the benchmark system prompt and user question for NatureLM."""
    return f"{bench.ANSWER_SYSTEM}\n\n{question['prompt']}"


def clean_naturelm_output(raw: str) -> str:
    """Remove the official inference API's per-window timestamp wrappers."""
    lines = [_WINDOW_PREFIX.sub("", line) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line.strip()).strip()


def select_device(requested: str) -> str:
    """Select a PyTorch device without importing torch at module import time."""
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_audio_paths(questions: list[dict[str, Any]]) -> None:
    """Validate and hash the exact MP3 bytes used by the hosted audio models."""
    for question in questions:
        for audio in question.get("audio", []):
            path = ROOT / audio["local_path"]
            if not path.exists():
                raise RuntimeError(
                    f"audio file missing: {path}; run `poe resolve-audio` or "
                    "`python bench.py resolve-audio --questions audio_questions.json` first"
                )
            audio["mp3_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    args = parse_args()

    questions_path = Path(args.questions)
    if not questions_path.is_absolute():
        questions_path = ROOT / questions_path
    qdata = json.loads(questions_path.read_text())
    questions = qdata["questions"]
    if args.question_id:
        questions = [q for q in questions if q["id"] == args.question_id]
        if not questions:
            raise SystemExit(f"unknown question id: {args.question_id}")

    load_audio_paths(questions)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    answers_dir = out_dir / "answers"
    answers_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = out_dir / "naturelm-receipts.jsonl"
    answer_path = answers_dir / f"{bench.slug(MODEL_ID)}.json"

    record = (
        json.loads(answer_path.read_text())
        if answer_path.exists()
        else {
            "model": MODEL_ID,
            "answers": {},
        }
    )

    # These imports intentionally occur after benchmark-only argument validation:
    # the repository remains stdlib-only unless this optional runner is used.
    from NatureLM.infer import load_model_and_config, sliding_window_inference
    from NatureLM.processors import NatureLMAudioProcessor

    device = select_device(args.device)
    import torch

    torch.manual_seed(42)
    model, cfg = load_model_and_config(device=device)
    cfg.generate.max_new_tokens = args.max_new_tokens
    cfg.generate.merging_alpha = args.merging_alpha

    processor = NatureLMAudioProcessor(sample_rate=16_000, max_length_seconds=10)
    print(f"Loaded {MODEL_ID} on {device}", flush=True)

    for question in questions:
        qid = question["id"]
        if qid in record["answers"] and not record["answers"][qid].get("error"):
            continue

        audio = question["audio"][0]
        audio_path = (ROOT / audio["local_path"]).resolve()
        query = build_query(question)
        started = time.perf_counter()
        prompt_tokens = len(model.llama_tokenizer(query, add_special_tokens=False)["input_ids"])
        try:
            raw = sliding_window_inference(
                audio=str(audio_path),
                query=query,
                processor=processor,
                model=model,
                cfg=cfg,
                window_length_seconds=args.window_seconds,
                hop_length_seconds=args.hop_seconds,
                input_sr=16_000,
                device=device,
            )
            final = clean_naturelm_output(raw)
            completion_tokens = len(model.llama_tokenizer(final, add_special_tokens=False)["input_ids"])
            error = None if final else "incomplete final output"
            finish_reason = "stop" if final else "length"
        except Exception as exc:
            raw = ""
            final = ""
            completion_tokens = 0
            error = f"{type(exc).__name__}: {exc}"
            finish_reason = "error"

        elapsed = time.perf_counter() - started

        receipt = {
            "stage": "generate-local",
            "model": MODEL_ID,
            "question_id": qid,
            "prompt": question["prompt"],
            "system": bench.ANSWER_SYSTEM,
            "rendered_query": query,
            "prompt_tokens": prompt_tokens,
            "mp3_sha256": audio["mp3_sha256"],
            "audio_path": str(audio_path),
            "raw_text": raw,
            "final_text": final,
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "elapsed_s": elapsed,
            "device": device,
            "window_seconds": args.window_seconds,
            "hop_seconds": args.hop_seconds,
            "merging_alpha": args.merging_alpha,
            "max_new_tokens": args.max_new_tokens,
            "seed": 42,
            "error": error,
        }
        with receipt_path.open("a") as f:
            f.write(json.dumps(receipt) + "\n")

        record["answers"][qid] = {
            "text": final,
            "elapsed_s": elapsed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "used_reasoning_field": False,
            "error": error,
        }
        answer_path.write_text(json.dumps(record, indent=2) + "\n")
        print(
            qid,
            f"{elapsed:.1f}s",
            f"{completion_tokens} tokens",
            "ERROR" if error else "ok",
            flush=True,
        )

        del raw, final
        gc.collect()
        if device == "mps":
            torch.mps.empty_cache()
        elif device == "cuda":
            torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
