#!/usr/bin/env python3
"""Survival benchmark runner. Targets an OpenAI-compatible endpoint (LM Studio)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUESTIONS_PATH = ROOT / "questions.json"
RESULTS_DIR = ROOT / "results"
ANSWERS_DIR = RESULTS_DIR / "answers"
JUDGMENTS_DIR = RESULTS_DIR / "judgments"
ENV_FILE = ROOT / ".env"
DEFAULT_BASE = "http://localhost:1234/v1"
DEFAULT_TIMEOUT = 600
REASONING_EFFORT_CHOICES = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file()
ENV_BASE = os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE
ENV_KEY = os.environ.get("OPENAI_API_KEY") or ""

ANSWER_SYSTEM = (
    "You are a survival and wilderness-medicine assistant. Answer the user's "
    "question with practical, accurate, safety-first guidance. Be concise but "
    "complete. Do not invent information; if a topic requires professional help, "
    "say so explicitly."
)

JUDGE_SYSTEM = (
    "You are a strict evaluator of survival-advice answers. For each criterion "
    "you receive, judge whether the candidate response satisfies it. Reply with "
    "exactly one line: 'YES' or 'NO' followed by a colon and one short sentence "
    "explaining why. Do not output anything else."
)

JUDGE_TEMPLATE = """\
QUESTION:
{question}
{ground_truth_block}
CANDIDATE RESPONSE:
{response}

CRITERION TYPE: {ctype}
CRITERION: {criterion}

Did the candidate response satisfy this criterion?
- For 'must_include' / 'bonus': YES if the response clearly conveys the point (paraphrase fine; partial credit NO).
- For 'must_not_include': YES if the response COMMITS the violation described (i.e. the bad behavior IS present). NO if the response avoids the violation.

Reply on one line: 'YES: reason' or 'NO: reason'."""


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def _auth_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
        # OpenRouter recommends these for tracking, harmless elsewhere
        h["HTTP-Referer"] = "https://github.com/local/survival-bench"
        h["X-Title"] = "survival-bench"
    return h


def http_post_json(url: str, payload: dict, *, api_key: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_auth_headers(api_key), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(url: str, *, api_key: str = "", timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=_auth_headers(api_key), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


TRANSIENT_EXCEPTIONS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
    TimeoutError,
    json.JSONDecodeError,
)


def http_post_json_retry(
    url: str, payload: dict, *, api_key: str = "", timeout: int = DEFAULT_TIMEOUT, attempts: int = 3
) -> dict:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return http_post_json(url, payload, api_key=api_key, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 4xx (except 429) is non-retryable
            if e.code == 429 or 500 <= e.code < 600:
                last = e
            else:
                raise
        except TRANSIENT_EXCEPTIONS as e:
            last = e
        if i < attempts - 1:
            time.sleep(2**i)
    assert last is not None
    raise last


def list_models(base: str, api_key: str = "") -> list[str]:
    data = http_get_json(f"{base}/models", api_key=api_key)
    out = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if "embed" in mid.lower():
            continue
        out.append(mid)
    return out


def chat(
    base: str,
    model: str,
    system: str,
    user: str,
    *,
    images: list[str] | None = None,
    audio: list[tuple[str, str]] | None = None,  # list of (base64_data, format)
    api_key: str = "",
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: int = DEFAULT_TIMEOUT,
    reasoning_effort: str = "",
    provider_order: list[str] | None = None,
) -> tuple[str, dict]:
    if images or audio:
        user_content: list[dict] | str = [{"type": "text", "text": user}]
        for url in images or []:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
        for data, fmt in audio or []:
            user_content.append({"type": "input_audio", "input_audio": {"data": data, "format": fmt}})
    else:
        user_content = user
    # Newer OpenAI models (gpt-5+, o1+, o3+) require `max_completion_tokens` and
    # reject `max_tokens` outright. Detect by model id.
    token_field = "max_completion_tokens" if re.match(r"^(gpt-[5-9]|o[1-9])", model) else "max_tokens"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        token_field: max_tokens,
        "stream": False,
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    if provider_order:
        payload["provider"] = {"order": provider_order, "allow_fallbacks": False}
    t0 = time.time()
    resp = http_post_json_retry(f"{base}/chat/completions", payload, api_key=api_key, timeout=timeout)
    elapsed = time.time() - t0
    if "choices" not in resp or not resp["choices"]:
        raise RuntimeError(f"no choices in response: {json.dumps(resp)[:300]}")
    choice = resp["choices"][0]
    msg = choice["message"]
    text = msg.get("content") or ""
    used_reasoning = False
    if not text.strip():
        # Reasoning models may put final content in reasoning_content if budget ran out
        rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if rc.strip():
            text = rc
            used_reasoning = True
    usage = resp.get("usage", {})
    meta = {
        "elapsed_s": round(elapsed, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "used_reasoning_field": used_reasoning,
        "finish_reason": choice.get("finish_reason"),
    }
    return text, meta


def load_questions(path: Path | None = None) -> dict:
    p = path or QUESTIONS_PATH
    return json.loads(p.read_text())


WIKI_API = "https://en.wikipedia.org/w/api.php"


def resolve_wiki_image(page: str) -> str:
    """Fetch the canonical lead-image URL for a Wikipedia article via MediaWiki action API."""
    qs = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "prop": "pageimages",
            "titles": page,
            "piprop": "original",
            "redirects": "1",
        }
    )
    url = f"{WIKI_API}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "survival-bench/1.0 (https://github.com/local/survival-bench)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    pages = (data.get("query") or {}).get("pages") or {}
    for _pid, info in pages.items():
        orig = info.get("original")
        if orig and orig.get("source"):
            return orig["source"]
    raise RuntimeError(f"no image found for Wikipedia page {page!r}")


WIKI_FILE_INFO = "https://commons.wikimedia.org/w/api.php"


def resolve_wiki_file_url(file_title: str) -> str:
    """Resolve a Wikimedia 'File:foo.ogg' title to its canonical download URL."""
    qs = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "titles": file_title,
            "iiprop": "url",
        }
    )
    url = f"{WIKI_FILE_INFO}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "survival-bench/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for _pid, info in (data.get("query") or {}).get("pages", {}).items():
        for ii in info.get("imageinfo") or []:
            if ii.get("url"):
                return ii["url"]
    raise RuntimeError(f"no URL found for {file_title!r}")


def _convert_to_mp3(src: Path, dst: Path) -> None:
    """Convert any audio file to mono 22kHz mp3, hard-trim to 20s, ~64 kbps."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "22050",
            "-t",
            "20",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def _xc_download_url(xc_id: str) -> str:
    return f"https://xeno-canto.org/{xc_id}/download"


def cmd_resolve_audio(args: argparse.Namespace) -> None:
    """Download referenced audio files and convert to MP3 in audio_clips/.

    Each audio entry can specify EITHER:
      - wiki_file: 'File:foo.ogg' (Wikimedia)
      - xc_id: '1234567' (xeno-canto recording id; requires XENOCANTO_API_KEY in env)

    Output is `audio_clips/<question_id>_<i>.mp3`, recorded back into the JSON as `local_path`.
    """
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found; install with `brew install ffmpeg` (or apt install ffmpeg)")
    xc_key = os.environ.get("XENOCANTO_API_KEY", "")
    path = Path(args.questions)
    qdata = json.loads(path.read_text())
    clips_dir = ROOT / "audio_clips"
    clips_dir.mkdir(exist_ok=True)
    for q in qdata["questions"]:
        for i, a in enumerate(q.get("audio", []) or []):
            if a.get("local_path") and (ROOT / a["local_path"]).exists():
                continue
            try:
                if a.get("xc_id"):
                    if not xc_key:
                        raise RuntimeError("XENOCANTO_API_KEY not set in env / .env")
                    xc_id = str(a["xc_id"])
                    src_url = f"{_xc_download_url(xc_id)}?key={xc_key}"
                    label = f"xc:{xc_id}"
                elif a.get("wiki_file"):
                    src_url = resolve_wiki_file_url(a["wiki_file"])
                    label = a["wiki_file"]
                else:
                    continue

                req = urllib.request.Request(src_url, headers={"User-Agent": "survival-bench/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                # detect file extension from content-type or URL
                ext = (
                    "mp3"
                    if "mpeg" in ctype
                    else "ogg"
                    if "ogg" in ctype
                    else src_url.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
                )
                if ext not in ("mp3", "ogg", "wav", "flac", "oga"):
                    ext = "mp3"  # xc downloads default to mp3
                tmp = clips_dir / f"_tmp_{q['id']}_{i}.{ext}"
                tmp.write_bytes(raw)
                mp3 = clips_dir / f"{q['id']}_{i}.mp3"
                _convert_to_mp3(tmp, mp3)  # always re-encode to enforce 20s trim + mono 22kHz
                tmp.unlink()
                a["local_path"] = str(mp3.relative_to(ROOT))
                a["source_url"] = src_url.split("?")[0]  # don't store key in JSON
                size_kb = mp3.stat().st_size // 1024
                print(f"  {q['id']}: {label} -> {a['local_path']} ({size_kb} KB)")
            except Exception as e:
                print(f"  {q['id']}: FAILED: {e}")
    path.write_text(json.dumps(qdata, indent=2))
    print(f"\nwrote {path}")


def cmd_resolve_images(args: argparse.Namespace) -> None:
    """Populate image_url fields in a vision questions file via Wikipedia API."""
    path = Path(args.questions)
    qdata = json.loads(path.read_text())
    changed = 0
    for q in qdata["questions"]:
        for img in q.get("images", []) or []:
            if img.get("image_url"):
                continue
            page = img.get("wiki_page")
            if not page:
                continue
            try:
                img["image_url"] = resolve_wiki_image(page)
                changed += 1
                print(f"  {q['id']}: {page} -> {img['image_url']}")
            except Exception as e:
                print(f"  {q['id']}: {page} FAILED: {e}")
    path.write_text(json.dumps(qdata, indent=2))
    print(f"\nresolved {changed} new image URLs; wrote {path}")


def cmd_models(args: argparse.Namespace) -> None:
    for m in list_models(args.base, args.api_key):
        print(m)


def _result_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    base = Path(args.out_dir) if args.out_dir else RESULTS_DIR
    return base / "answers", base / "judgments"


CONFIG_KEYS = ("reasoning_effort", "max_tokens", "temperature", "provider_order", "samples")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_run_config(args: argparse.Namespace, model: str, qpath: Path) -> dict:
    """Run configuration for one model. Records the endpoint host only, never keys or full URLs."""
    return {
        "model": model,
        "endpoint_host": urllib.parse.urlparse(args.base).hostname or "",
        "reasoning_effort": getattr(args, "reasoning_effort", "") or "unset",
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "provider_order": getattr(args, "provider_order", "") or "",
        "samples": getattr(args, "samples", 1) or 1,
        "label": getattr(args, "label", "") or "",
        "questions_file": qpath.name,
        "questions_sha256": file_sha256(qpath),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_manifest(manifests_dir: Path, config: dict) -> None:
    """Write manifests/<model>.json; keep a history of runs and warn when the config changes."""
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{slug(config['model'])}.json"
    runs = []
    if path.exists():
        prev = json.loads(path.read_text())
        runs = prev.get("runs", [])
        if runs and any(runs[-1].get(k) != config.get(k) for k in CONFIG_KEYS):
            print(
                f"WARNING: {config['model']} config differs from its previous run in this directory; "
                "answers may mix configurations",
                flush=True,
            )
    runs.append(config)
    path.write_text(json.dumps({**config, "runs": runs}, indent=2))


def load_run_configs(out_dir: Path) -> dict[str, dict]:
    """Per-model run configs from manifests/, falling back to a legacy run-manifest.json."""
    configs: dict[str, dict] = {}
    legacy = out_dir / "run-manifest.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
        except json.JSONDecodeError:
            data = {}
        for m in data.get("models", []) if isinstance(data, dict) else []:
            if isinstance(m, dict) and m.get("id"):
                configs[m["id"]] = {
                    "model": m["id"],
                    "reasoning_effort": m.get("reasoning_effort") or "unset",
                    "max_tokens": m.get("max_tokens"),
                    "temperature": m.get("temperature", data.get("temperature")),
                    "label": m.get("label", ""),
                }
    for mf in sorted((out_dir / "manifests").glob("*.json")):
        rec = json.loads(mf.read_text())
        if rec.get("model"):
            configs[rec["model"]] = rec
    return configs


def config_mismatches(configs: list[dict]) -> list[str]:
    """Describe fields that differ across the given run configs (empty list = comparable)."""
    out = []
    for key in ("reasoning_effort", "max_tokens", "temperature"):
        vals = {c.get("model", "?"): c.get(key) for c in configs if c.get(key) is not None}
        if len(set(map(str, vals.values()))) > 1:
            out.append(f"{key}: " + ", ".join(f"`{m}`={v}" for m, v in vals.items()))
    return out


def is_truncated(ans: dict) -> bool:
    """True if an answer hit the token cap or produced no final content."""
    if ans.get("error"):
        return False
    return (
        ans.get("finish_reason") == "length"
        or not (ans.get("text") or "").strip()
        or bool(ans.get("used_reasoning_field"))
    )


def format_config(cfg: dict | None) -> str:
    if not cfg:
        return "—"
    parts = [f"reasoning={cfg.get('reasoning_effort', 'unset')}", f"max={cfg.get('max_tokens', '?')}"]
    if cfg.get("label"):
        parts.append(str(cfg["label"]))
    return " / ".join(parts)


PENDING = "pending"
SCORE_FIELDS = ("correctness", "bonus_rate", "safety_violations", "composite")


def answer_samples(record: dict) -> dict[str, list[dict]]:
    """Answers per question as a list indexed by sample. Legacy files hold one dict per question."""
    return {qid: list(v) if isinstance(v, list) else [v] for qid, v in (record.get("answers") or {}).items()}


def judgment_samples(entry: dict) -> list[dict]:
    """Per-sample judgments for one question. A legacy entry is itself the single sample."""
    if not entry:
        return []
    return list(entry["samples"]) if "samples" in entry else [entry]


def aggregate_scores(scores: list[dict]) -> dict:
    """Per-question score = mean of each field across samples (formula per sample is unchanged)."""
    if len(scores) == 1:
        return dict(scores[0])
    n = len(scores)
    return {f: round(sum(s.get(f, 0) for s in scores) / n, 3) for f in SCORE_FIELDS}


def fmt_count(x: float) -> str:
    """Violation counts are whole numbers with one sample and per-sample means otherwise."""
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def count_truncated(answers_path: Path) -> int | str:
    if not answers_path.exists():
        return "—"
    record = json.loads(answers_path.read_text())
    return sum(1 for samples in answer_samples(record).values() for ans in samples if is_truncated(ans))


def cmd_generate(args: argparse.Namespace) -> None:
    qpath = Path(args.questions) if args.questions else QUESTIONS_PATH
    qdata = load_questions(qpath)
    questions = qdata["questions"]
    answers_dir, _ = _result_dirs(args)
    print(f"Questions file: {qpath}")
    print(f"Output dir: {answers_dir.parent}")
    if args.limit:
        questions = questions[: args.limit]
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = list_models(args.base, args.api_key)
    answers_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        write_manifest(answers_dir.parent / "manifests", build_run_config(args, model, qpath))

    print(f"Endpoint: {args.base}")
    print(f"Models ({len(models)}): {', '.join(models)}")
    print(f"Questions: {len(questions)}")
    print(f"Concurrency: {args.concurrency}")
    print()

    # Build pending work and per-model state
    n_samples = max(1, getattr(args, "samples", 1) or 1)
    answers_by_model: dict[str, dict[str, list[dict]]] = {}
    paths: dict[str, Path] = {}
    locks: dict[str, threading.Lock] = {}
    pending: list[tuple[str, dict, int]] = []
    for model in models:
        out_path = answers_dir / f"{slug(model)}.json"
        paths[model] = out_path
        locks[model] = threading.Lock()
        if out_path.exists() and args.resume:
            answers_by_model[model] = answer_samples(json.loads(out_path.read_text()))
        else:
            answers_by_model[model] = {}
        for q in questions:
            slots = answers_by_model[model].setdefault(q["id"], [])
            while len(slots) < n_samples:
                slots.append({"text": "", "error": PENDING})
            for idx, cur in enumerate(slots):
                if not cur.get("error") and (cur.get("text") or "").strip():
                    continue
                pending.append((model, q, idx))

    if not pending:
        print("nothing to do (all answers cached)")
        return
    print(f"Samples per question: {n_samples}")
    print(f"Pending: {len(pending)} (model, question, sample) answers")

    def write_answers(model: str) -> None:
        paths[model].write_text(
            json.dumps({"model": model, "format": 2, "answers": answers_by_model[model]}, indent=2)
        )

    done = 0
    total = len(pending)
    start = time.time()

    def work(model: str, q: dict, idx: int) -> tuple[str, str, int, dict]:
        try:
            image_urls = [img["image_url"] for img in q.get("images", []) or [] if img.get("image_url")]
            audio_payload: list[tuple[str, str]] = []
            for a in q.get("audio", []) or []:
                lp = a.get("local_path")
                if not lp:
                    continue
                p = ROOT / lp
                if not p.exists():
                    raise RuntimeError(f"audio file missing: {p}")
                fmt = p.suffix.lstrip(".").lower() or "mp3"
                audio_payload.append((base64.b64encode(p.read_bytes()).decode("ascii"), fmt))
            text, meta = chat(
                args.base,
                model,
                ANSWER_SYSTEM,
                q["prompt"],
                images=image_urls or None,
                audio=audio_payload or None,
                api_key=args.api_key,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                reasoning_effort=getattr(args, "reasoning_effort", "") or "",
                provider_order=(
                    [p.strip() for p in (getattr(args, "provider_order", "") or "").split(",") if p.strip()]
                    or None
                ),
            )
            return model, q["id"], idx, {"text": text, **meta, "error": None}
        except Exception as e:
            return model, q["id"], idx, {"text": "", "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(work, m, q, idx) for m, q, idx in pending]
        for fut in as_completed(futures):
            model, qid, idx, result = fut.result()
            with locks[model]:
                answers_by_model[model][qid][idx] = result
                write_answers(model)
            done += 1
            err = result.get("error")
            tag = (
                f"ERROR {err}" if err else f"{result.get('elapsed_s')}s {result.get('completion_tokens')}tok"
            )
            print(f"  [{done}/{total}] {model}  {qid}#{idx}  {tag}", flush=True)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")
    for p in paths.values():
        print(f"  -> {p}")


def parse_judge_line(line: str) -> tuple[str, str]:
    line = line.strip()
    upper = line.upper()
    if upper.startswith("YES"):
        return "YES", line.split(":", 1)[1].strip() if ":" in line else ""
    if upper.startswith("NO"):
        return "NO", line.split(":", 1)[1].strip() if ":" in line else ""
    # try to find embedded
    m = re.search(r"\b(YES|NO)\b", upper)
    if m:
        v = m.group(1)
        return v, line
    return "NO", f"unparseable: {line[:120]}"


def judge_response(
    base: str,
    judge_model: str,
    question: str,
    response: str,
    ctype: str,
    criterion: str,
    *,
    api_key: str = "",
    ground_truth: str = "",
) -> dict:
    gt_block = (
        f"\nGROUND TRUTH (the actual species/condition shown — use to interpret criteria):\n{ground_truth}\n"
        if ground_truth
        else ""
    )
    user = JUDGE_TEMPLATE.format(
        question=question,
        response=response,
        ctype=ctype,
        criterion=criterion,
        ground_truth_block=gt_block,
    )
    text, meta = chat(
        base,
        judge_model,
        JUDGE_SYSTEM,
        user,
        api_key=api_key,
        temperature=0.0,
        max_tokens=128,
        timeout=DEFAULT_TIMEOUT,
    )
    # Some models prepend reasoning_content or extra prose; take first non-empty line.
    first = next((ln for ln in text.splitlines() if ln.strip()), text)
    verdict, reason = parse_judge_line(first)
    return {"criterion": criterion, "verdict": verdict, "reason": reason, "raw": text.strip()}


def score_question(judgment: dict) -> dict:
    mi = judgment["must_include"]
    mn = judgment["must_not_include"]
    bn = judgment["bonus"]
    mi_hits = sum(1 for x in mi if x["verdict"] == "YES")
    mn_violations = sum(1 for x in mn if x["verdict"] == "YES")
    bn_hits = sum(1 for x in bn if x["verdict"] == "YES")
    correctness = mi_hits / len(mi) if mi else 0.0
    bonus_rate = bn_hits / len(bn) if bn else 0.0
    # composite: correctness penalized by safety violations on this Q
    safety_pen = mn_violations * 0.5  # each violation removes half a point
    composite = max(-1.0, correctness + 0.25 * bonus_rate - safety_pen)
    return {
        "correctness": round(correctness, 3),
        "bonus_rate": round(bonus_rate, 3),
        "safety_violations": mn_violations,
        "composite": round(composite, 3),
    }


CRITERION_TYPES = ("must_include", "must_not_include", "bonus")


def sample_fully_judged(judgment: dict | None, expected: int) -> bool:
    """A cached sample judgment can be reused if it has no error and every criterion present."""
    if not judgment or judgment.get("error"):
        return False
    return sum(len(judgment.get(c, [])) for c in CRITERION_TYPES) >= expected


def judgment_entry(slots: list[dict]) -> dict:
    """Stored form of one question's judgments; the mean score appears once every sample is scored."""
    entry: dict = {"samples": slots}
    scores = [sl["score"] for sl in slots if "score" in sl]
    if slots and len(scores) == len(slots):
        entry["score"] = aggregate_scores(scores)
    return entry


def cmd_judge(args: argparse.Namespace) -> None:
    qpath = Path(args.questions) if args.questions else QUESTIONS_PATH
    qdata = load_questions(qpath)
    questions = {q["id"]: q for q in qdata["questions"]}
    answers_dir, judgments_dir = _result_dirs(args)
    if not answers_dir.exists():
        sys.exit(f"no answers directory at {answers_dir}; run generate first")
    judgments_dir.mkdir(parents=True, exist_ok=True)

    judge_model = args.judge_model
    if not judge_model:
        models = list_models(args.base, args.api_key)
        # heuristic: pick the largest by parameter count token in the name
        ranked = sorted(models, key=lambda m: _size_hint(m), reverse=True)
        judge_model = ranked[0]
    print(f"Judge model: {judge_model}")

    # Build the full task list across all models. Each answer sample gets its own judgment
    # slot, tagged with its sample index so results route back and resume stays aligned.
    judgments_by_model: dict[str, dict[str, list[dict]]] = {}
    slots_by_key: dict[tuple[str, str, int], dict] = {}
    out_paths: dict[str, Path] = {}
    locks: dict[str, threading.Lock] = {}
    tasks: list[tuple[str, str, int, str, str, str]] = []  # (model, qid, sample, answer, ctype, criterion)
    skipped_pending = 0

    for ans_file in sorted(answers_dir.glob("*.json")):
        record = json.loads(ans_file.read_text())
        model = record["model"]
        out_path = judgments_dir / ans_file.name
        out_paths[model] = out_path
        locks[model] = threading.Lock()
        existing: dict[str, dict] = {}
        if out_path.exists() and args.resume:
            prev_file = json.loads(out_path.read_text())
            if prev_file.get("judge") == judge_model:
                existing = prev_file.get("judgments", {})
        judgments_by_model[model] = {}

        for qid, samples in answer_samples(record).items():
            q = questions.get(qid)
            if not q:
                continue
            prev = {j.get("sample", 0): j for j in judgment_samples(existing.get(qid, {}))}
            expected = sum(len(q.get(c, [])) for c in CRITERION_TYPES)
            slots: list[dict] = []
            for sidx, ans in enumerate(samples):
                if ans.get("error") == PENDING:
                    skipped_pending += 1
                    continue
                if ans.get("error") or not ans.get("text"):
                    slot = {
                        "sample": sidx,
                        "error": ans.get("error", "empty"),
                        "must_include": [],
                        "must_not_include": [],
                        "bonus": [],
                        "score": {
                            "correctness": 0.0,
                            "bonus_rate": 0.0,
                            "safety_violations": 0,
                            "composite": -1.0,
                        },
                    }
                elif sample_fully_judged(prev.get(sidx), expected):
                    slot = {"sample": sidx, **prev[sidx]}
                else:
                    slot = {"sample": sidx, "must_include": [], "must_not_include": [], "bonus": []}
                    for ctype in CRITERION_TYPES:
                        for crit in q.get(ctype, []):
                            tasks.append((model, qid, sidx, ans["text"], ctype, crit))
                slots.append(slot)
                slots_by_key[(model, qid, sidx)] = slot
            if slots:
                judgments_by_model[model][qid] = slots

    if skipped_pending:
        print(f"WARNING: skipped {skipped_pending} answer samples still pending generation")
    if not tasks:
        print("nothing to judge")
    else:
        print(f"Pending: {len(tasks)} judge calls")

    def write(model: str) -> None:
        out_paths[model].write_text(
            json.dumps(
                {
                    "model": model,
                    "judge": judge_model,
                    "format": 2,
                    "judgments": {qid: judgment_entry(sl) for qid, sl in judgments_by_model[model].items()},
                },
                indent=2,
            )
        )

    # initial flush so empty-answer cases are saved
    judgments_dir.mkdir(parents=True, exist_ok=True)
    for m in out_paths:
        with locks[m]:
            write(m)

    def judge_one(
        model: str, qid: str, sidx: int, answer_text: str, ctype: str, crit: str
    ) -> tuple[str, str, int, str, dict]:
        q = questions[qid]
        try:
            res = judge_response(
                args.base,
                judge_model,
                q["prompt"],
                answer_text,
                ctype,
                crit,
                api_key=args.api_key,
                ground_truth=q.get("ground_truth", ""),
            )
        except Exception as e:
            res = {
                "criterion": crit,
                "verdict": "NO",
                "reason": f"judge-error: {type(e).__name__}: {e}",
                "raw": "",
            }
        return model, qid, sidx, ctype, res

    done = 0
    total = len(tasks)
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(judge_one, *t) for t in tasks]
        for fut in as_completed(futures):
            model, qid, sidx, ctype, res = fut.result()
            with locks[model]:
                slots_by_key[(model, qid, sidx)][ctype].append(res)
            done += 1
            if done % 25 == 0 or done == total:
                # flush all files periodically
                for m in out_paths:
                    with locks[m]:
                        write(m)
                print(f"  [{done}/{total}] {time.time() - start:.0f}s elapsed", flush=True)

    # finalize: compute scores per sample (and the per-question mean), write
    for model, judgments in judgments_by_model.items():
        for slots in judgments.values():
            for slot in slots:
                if "score" not in slot:
                    slot["score"] = score_question(slot)
        with locks[model]:
            write(model)
        print(f"  -> {out_paths[model]}")


def _size_hint(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]", model)
    return float(m.group(1)) if m else 0.0


BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20260906


def question_composites(record: dict) -> dict[str, float]:
    """Per-question composite (already the mean across samples) for one judgments file."""
    return {qid: j.get("score", {}).get("composite", 0) for qid, j in record["judgments"].items() if j}


def bootstrap_ci(
    values: list[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean, resampling values with replacement."""
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    k = len(values)
    means = sorted(sum(rng.choices(values, k=k)) / k for _ in range(resamples))
    lo = means[int(alpha / 2 * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)


def fmt_signed(x: float) -> str:
    """Signed 2-dp value that never renders as '-0.00'."""
    return "0.00" if round(x, 2) == 0 else f"{x:+.2f}"


def fmt_ci(ci: tuple[float, float]) -> str:
    return f"[{fmt_signed(ci[0])}, {fmt_signed(ci[1])}]"


def paired_comparison(a: dict[str, float], b: dict[str, float], *, top: int = 5) -> dict:
    """Paired per-question difference A − B over the questions both models completed."""
    shared = sorted(set(a) & set(b))
    diffs = {qid: a[qid] - b[qid] for qid in shared}
    vals = list(diffs.values())
    ranked = sorted(diffs.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "n": len(shared),
        "only_a": len(set(a) - set(b)),
        "only_b": len(set(b) - set(a)),
        "mean_diff": sum(vals) / len(vals) if vals else float("nan"),
        "ci": bootstrap_ci(vals),
        "wins": sum(1 for d in vals if round(d, 3) > 0),
        "ties": sum(1 for d in vals if round(d, 3) == 0),
        "losses": sum(1 for d in vals if round(d, 3) < 0),
        "top_a": [kv for kv in ranked if round(kv[1], 3) > 0][:top],
        "top_b": [kv for kv in reversed(ranked) if round(kv[1], 3) < 0][:top],
    }


def format_comparison(a: str, b: str, res: dict, questions: dict, mismatches: list[str]) -> list[str]:
    lo, hi = res["ci"]
    lines = [f"## Paired comparison: `{a}` − `{b}`\n"]
    if mismatches:
        lines.append("> ⚠️ **Config mismatch** — " + "; ".join(mismatches))
        lines.append("")
    lines.append(f"- Questions both completed: **{res['n']}**")
    if res["only_a"] or res["only_b"]:
        lines.append(f"- Excluded: {res['only_a']} only in `{a}`, {res['only_b']} only in `{b}`")
    lines.append(
        f"- Mean composite difference: **{fmt_signed(res['mean_diff'])}** {fmt_ci(res['ci'])} (95% CI)"
    )
    lines.append(f"- Win / tie / loss for `{a}`: {res['wins']} / {res['ties']} / {res['losses']}")
    if lo <= 0 <= hi:
        lines.append("- **Difference not distinguishable at this sample size** (the 95% CI includes 0).")
    lines.append("")
    for title, items in ((f"`{a}` ahead", res["top_a"]), (f"`{b}` ahead", res["top_b"])):
        lines.append(f"**Largest differences — {title}**\n")
        if not items:
            lines.append("_None._\n")
            continue
        lines.append("| Question | Category | Difference |")
        lines.append("|---|---|---:|")
        for qid, d in items:
            lines.append(f"| {qid} | {questions.get(qid, {}).get('category', '—')} | {d:+.2f} |")
        lines.append("")
    return lines


def cmd_report(args: argparse.Namespace) -> None:
    qpath = Path(args.questions) if args.questions else QUESTIONS_PATH
    qdata = load_questions(qpath)
    questions = {q["id"]: q for q in qdata["questions"]}
    categories = sorted({q["category"] for q in qdata["questions"]})
    answers_dir, judgments_dir = _result_dirs(args)
    out_dir = judgments_dir.parent
    configs = load_run_configs(out_dir)

    rows = []
    for jf in sorted(judgments_dir.glob("*.json")):
        rec = json.loads(jf.read_text())
        rows.append(rec)
    if not rows:
        sys.exit("no judgments yet; run judge first")

    out_lines: list[str] = []
    out_lines.append("# Survival Benchmark — Results\n")
    out_lines.append(f"- Questions: **{len(questions)}** across {len(categories)} categories")
    out_lines.append(f"- Models evaluated: **{len(rows)}**")
    judge_models = {r.get("judge") for r in rows}
    out_lines.append(f"- Judge model(s): {', '.join(sorted(j for j in judge_models if j))}")
    out_lines.append("")
    mismatches = config_mismatches([configs[r["model"]] for r in rows if r["model"] in configs])
    if len([j for j in judge_models if j]) > 1:
        mismatches.append("judge: " + ", ".join(f"`{r['model']}`={r.get('judge')}" for r in rows))
    if mismatches:
        out_lines.append("> ⚠️ **Config mismatch** — these models were not run under the same settings, so")
        out_lines.append("> score differences may reflect configuration rather than the model:")
        for mm in mismatches:
            out_lines.append(f"> - {mm}")
        out_lines.append("")
        print("WARNING: config mismatch between compared models:", file=sys.stderr)
        for mm in mismatches:
            print(f"  - {mm}", file=sys.stderr)
    out_lines.append("**Scoring** — each question:")
    out_lines.append("- *Correctness* = fraction of `must_include` criteria satisfied")
    out_lines.append(
        "- *Safety violations* = count of `must_not_include` criteria triggered (lower is better)"
    )
    out_lines.append("- *Bonus rate* = fraction of `bonus` criteria satisfied")
    out_lines.append("- *Composite* per question = correctness + 0.25·bonus − 0.5·violations (clipped at −1)")
    out_lines.append("")

    # --- Overall table ---
    out_lines.append("## Overall\n")
    out_lines.append(
        "| Model | Composite [95% CI] | Correctness | Safety viol. | Bonus | Q failed | Truncated/empty "
        "| Config |"
    )
    out_lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    summary = []
    for r in rows:
        scores = [j.get("score", {}) for j in r["judgments"].values() if j]
        if not scores:
            continue
        n = len(scores)
        comp = sum(s.get("composite", 0) for s in scores) / n
        corr = sum(s.get("correctness", 0) for s in scores) / n
        viols = sum(s.get("safety_violations", 0) for s in scores)
        bonus = sum(s.get("bonus_rate", 0) for s in scores) / n
        failed = sum(1 for s in scores if s.get("composite", 0) < 0)
        summary.append((r["model"], comp, corr, viols, bonus, failed, r))
    summary.sort(key=lambda x: x[1], reverse=True)
    truncated = {m: count_truncated(answers_dir / f"{slug(m)}.json") for m, *_ in summary}
    for m, comp, corr, viols, bonus, failed, rec in summary:
        out_lines.append(
            f"| `{m}` | {comp:+.2f} {fmt_ci(bootstrap_ci(list(question_composites(rec).values())))} "
            f"| {corr:.0%} | {fmt_count(viols)} | {bonus:.0%} | {failed} "
            f"| {truncated[m]} | {format_config(configs.get(m))} |"
        )
    out_lines.append("")
    out_lines.append(
        f"_95% CIs are percentile bootstraps over questions ({BOOTSTRAP_RESAMPLES} resamples, "
        f"seed {BOOTSTRAP_SEED}); they reflect question sampling, not judge error._"
    )
    out_lines.append("")
    out_lines.append(
        "_Truncated/empty counts answers with `finish_reason=length`, no final content, or a "
        "reasoning-field fallback. Answers recorded before `finish_reason` was saved are counted "
        "only when empty or fallback._"
    )
    out_lines.append("")

    # --- Per category ---
    out_lines.append("## By Category — Correctness\n")
    header = "| Model | " + " | ".join(categories) + " |"
    sep = "|---|" + "|".join("---:" for _ in categories) + "|"
    out_lines.append(header)
    out_lines.append(sep)
    for m, *_, r in summary:
        cells = [f"`{m}`"]
        for cat in categories:
            qs_in_cat = [qid for qid, q in questions.items() if q["category"] == cat]
            if not qs_in_cat:
                cells.append("—")
                continue
            corrs = [
                r["judgments"].get(qid, {}).get("score", {}).get("correctness", 0)
                for qid in qs_in_cat
                if qid in r["judgments"]
            ]
            cells.append(f"{(sum(corrs) / len(corrs) if corrs else 0):.0%}")
        out_lines.append("| " + " | ".join(cells) + " |")
    out_lines.append("")

    # --- Safety violation table ---
    out_lines.append("## By Category — Safety Violations (count)\n")
    out_lines.append(header)
    out_lines.append(sep)
    for m, *_, r in summary:
        cells = [f"`{m}`"]
        for cat in categories:
            qs_in_cat = [qid for qid, q in questions.items() if q["category"] == cat]
            viols = sum(
                r["judgments"].get(qid, {}).get("score", {}).get("safety_violations", 0) for qid in qs_in_cat
            )
            cells.append(fmt_count(viols))
        out_lines.append("| " + " | ".join(cells) + " |")
    out_lines.append("")

    # --- Safety violations detail ---
    out_lines.append("## Safety Violations Detail\n")
    any_viol = False
    for m, *_, r in summary:
        per_model = []
        for qid, j in r["judgments"].items():
            samples = judgment_samples(j)
            for sj in samples:
                label = f"{qid} #{sj.get('sample', 0)}" if len(samples) > 1 else qid
                for crit in sj.get("must_not_include", []):
                    if crit["verdict"] == "YES":
                        per_model.append((label, crit["criterion"], crit["reason"]))
        if per_model:
            any_viol = True
            out_lines.append(f"### `{m}`\n")
            for qid, crit, reason in per_model:
                out_lines.append(f"- **{qid}** — violated: *{crit}*")
                out_lines.append(f"  - judge note: {reason}")
            out_lines.append("")
    if not any_viol:
        out_lines.append("_No safety-critical violations detected._\n")

    # --- Per-question breakdown ---
    out_lines.append("## Per-Question Composite Scores\n")
    qids = list(questions.keys())
    out_lines.append("| Question | " + " | ".join(f"`{m}`" for m, *_ in summary) + " |")
    out_lines.append("|---|" + "|".join("---:" for _ in summary) + "|")
    for qid in qids:
        cells = [f"{qid} ({questions[qid]['category']})"]
        for *_, r in summary:
            s = r["judgments"].get(qid, {}).get("score")
            cells.append(f"{s['composite']:+.2f}" if s else "—")
        out_lines.append("| " + " | ".join(cells) + " |")
    out_lines.append("")

    if getattr(args, "compare", None):
        a, b = args.compare
        by_model = {r["model"]: r for r in rows}
        missing = [m for m in (a, b) if m not in by_model]
        if missing:
            sys.exit(f"--compare: no judgments for {', '.join(missing)}")
        cmp_lines = format_comparison(
            a,
            b,
            paired_comparison(question_composites(by_model[a]), question_composites(by_model[b])),
            questions,
            config_mismatches([configs[m] for m in (a, b) if m in configs]),
        )
        out_lines.extend(cmp_lines)
        print("\n".join(cmp_lines))

    out_lines.append("---\n")
    out_lines.append(
        "_Note on judge bias: when the judge is one of the evaluated models, its own answers "
        "may be over-rated. Categories with safety violations are the most reliable signal — "
        "they are objective rule violations rather than judgment calls._\n"
    )

    report_path = Path(args.output) if getattr(args, "output", None) else out_dir / "report.md"
    report_path.write_text("\n".join(out_lines))
    print(f"wrote {report_path}")


def cmd_all(args: argparse.Namespace) -> None:
    cmd_generate(args)
    cmd_judge(args)
    cmd_report(args)


def main() -> None:
    p = argparse.ArgumentParser(description="Survival benchmark for any OpenAI-compatible LLM endpoint")
    p.add_argument("--base", default=ENV_BASE, help="OpenAI-compatible API base URL (env: OPENAI_BASE_URL)")
    p.add_argument("--api-key", default=ENV_KEY, help="API key, sent as Bearer token (env: OPENAI_API_KEY)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("models", help="list models from endpoint")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("generate", help="generate answers from each model")
    sp.add_argument("--questions", help="path to questions file (default questions.json)")
    sp.add_argument("--models", help="comma-separated model IDs (default: all chat models from /v1/models)")
    sp.add_argument("--limit", type=int, default=0, help="limit number of questions (debug)")
    sp.add_argument("--temperature", type=float, default=0.3)
    sp.add_argument("--max-tokens", type=int, default=4000)
    sp.add_argument("--concurrency", type=int, default=64, help="parallel API calls")
    sp.add_argument("--out-dir", help="results directory (default ./results)")
    sp.add_argument("--resume", action="store_true", help="skip models with complete answer files")
    sp.add_argument(
        "--reasoning-effort",
        default="",
        choices=REASONING_EFFORT_CHOICES,
        help="OpenRouter-style reasoning.effort; empty = no reasoning field sent",
    )
    sp.add_argument(
        "--provider-order",
        default="",
        help="comma-separated OpenRouter providers to pin (e.g. 'DeepInfra'); empty = default routing",
    )
    sp.add_argument(
        "--label", default="", help="free-text run note recorded in the manifest (e.g. 'local Q4_K_M')"
    )
    sp.add_argument("--samples", type=int, default=1, help="answers per question (scores average them)")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("judge", help="judge each answer with a judge model")
    sp.add_argument("--questions", help="path to questions file (default questions.json)")
    sp.add_argument("--judge-model", help="model ID for judging (default: largest available)")
    sp.add_argument("--concurrency", type=int, default=64, help="parallel API calls")
    sp.add_argument("--out-dir", help="results directory (default ./results)")
    sp.add_argument("--resume", action="store_true", help="skip already-judged models")
    sp.set_defaults(func=cmd_judge)

    sp = sub.add_parser("report", help="produce results/report.md")
    sp.add_argument("--questions", help="path to questions file (default questions.json)")
    sp.add_argument("--out-dir", help="results directory (default ./results)")
    sp.add_argument("--output", help="report path (default <out-dir>/report.md)")
    sp.add_argument(
        "--compare",
        nargs=2,
        metavar=("A", "B"),
        help="paired per-question comparison of model A against model B",
    )
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser(
        "resolve-images", help="populate image_url in a vision questions file via Wikipedia API"
    )
    sp.add_argument("--questions", required=True, help="path to vision questions file")
    sp.set_defaults(func=cmd_resolve_images)

    sp = sub.add_parser(
        "resolve-audio", help="download Wikimedia audio files and convert to mp3 for an audio questions file"
    )
    sp.add_argument("--questions", required=True, help="path to audio questions file")
    sp.set_defaults(func=cmd_resolve_audio)

    sp = sub.add_parser("all", help="generate, judge, and report")
    sp.add_argument("--questions", help="path to questions file (default questions.json)")
    sp.add_argument("--models", help="comma-separated model IDs")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--temperature", type=float, default=0.3)
    sp.add_argument("--max-tokens", type=int, default=4000)
    sp.add_argument("--concurrency", type=int, default=64)
    sp.add_argument("--judge-model")
    sp.add_argument("--out-dir")
    sp.add_argument("--resume", action="store_true")
    sp.add_argument(
        "--reasoning-effort",
        default="",
        choices=REASONING_EFFORT_CHOICES,
    )
    sp.add_argument("--provider-order", default="")
    sp.add_argument("--label", default="")
    sp.add_argument("--samples", type=int, default=1)
    sp.set_defaults(func=cmd_all)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
