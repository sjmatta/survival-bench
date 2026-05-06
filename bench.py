#!/usr/bin/env python3
"""Survival benchmark runner. Targets an OpenAI-compatible endpoint (LM Studio)."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
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
) -> tuple[str, dict]:
    if images or audio:
        user_content: list[dict] | str = [{"type": "text", "text": user}]
        for url in images or []:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
        for data, fmt in audio or []:
            user_content.append({"type": "input_audio", "input_audio": {"data": data, "format": fmt}})
    else:
        user_content = user
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    t0 = time.time()
    resp = http_post_json_retry(f"{base}/chat/completions", payload, api_key=api_key, timeout=timeout)
    elapsed = time.time() - t0
    if "choices" not in resp or not resp["choices"]:
        raise RuntimeError(f"no choices in response: {json.dumps(resp)[:300]}")
    msg = resp["choices"][0]["message"]
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

    print(f"Endpoint: {args.base}")
    print(f"Models ({len(models)}): {', '.join(models)}")
    print(f"Questions: {len(questions)}")
    print(f"Concurrency: {args.concurrency}")
    print()

    # Build pending work and per-model state
    answers_by_model: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    locks: dict[str, threading.Lock] = {}
    pending: list[tuple[str, dict]] = []
    for model in models:
        out_path = answers_dir / f"{slug(model)}.json"
        paths[model] = out_path
        locks[model] = threading.Lock()
        if out_path.exists() and args.resume:
            existing = json.loads(out_path.read_text())
            answers_by_model[model] = existing.get("answers", {})
        else:
            answers_by_model[model] = {}
        for q in questions:
            qid = q["id"]
            cur = answers_by_model[model].get(qid)
            if cur and not cur.get("error") and (cur.get("text") or "").strip():
                continue
            pending.append((model, q))

    if not pending:
        print("nothing to do (all answers cached)")
        return
    print(f"Pending: {len(pending)} (model, question) pairs")

    done = 0
    total = len(pending)
    start = time.time()

    def work(model: str, q: dict) -> tuple[str, str, dict]:
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
            )
            return model, q["id"], {"text": text, **meta, "error": None}
        except Exception as e:
            return model, q["id"], {"text": "", "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(work, m, q) for m, q in pending]
        for fut in as_completed(futures):
            model, qid, result = fut.result()
            with locks[model]:
                answers_by_model[model][qid] = result
                paths[model].write_text(
                    json.dumps({"model": model, "answers": answers_by_model[model]}, indent=2)
                )
            done += 1
            err = result.get("error")
            tag = (
                f"ERROR {err}" if err else f"{result.get('elapsed_s')}s {result.get('completion_tokens')}tok"
            )
            print(f"  [{done}/{total}] {model}  {qid}  {tag}", flush=True)

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

    # Build the full task list across all models
    judgments_by_model: dict[str, dict] = {}
    out_paths: dict[str, Path] = {}
    locks: dict[str, threading.Lock] = {}
    tasks: list[tuple[str, str, dict, str]] = []  # (model, qid, answer_text, criterion-key)
    # criterion-key encodes type+index so we can route the result back

    for ans_file in sorted(answers_dir.glob("*.json")):
        record = json.loads(ans_file.read_text())
        model = record["model"]
        out_path = judgments_dir / ans_file.name
        out_paths[model] = out_path
        locks[model] = threading.Lock()
        if out_path.exists() and args.resume:
            existing = json.loads(out_path.read_text())
            if existing.get("judge") == judge_model:
                judgments_by_model[model] = existing.get("judgments", {})
            else:
                judgments_by_model[model] = {}
        else:
            judgments_by_model[model] = {}

        for qid, ans in record["answers"].items():
            q = questions.get(qid)
            if not q:
                continue
            if ans.get("error") or not ans.get("text"):
                judgments_by_model[model][qid] = {
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
                continue
            existing_j = judgments_by_model[model].get(qid)
            if existing_j and not existing_j.get("error"):
                # already fully judged — skip if all criteria present
                expected = sum(len(q.get(c, [])) for c in ("must_include", "must_not_include", "bonus"))
                actual = sum(
                    len(existing_j.get(c, [])) for c in ("must_include", "must_not_include", "bonus")
                )
                if actual >= expected:
                    continue
            # initialize empty slots
            judgments_by_model[model][qid] = {"must_include": [], "must_not_include": [], "bonus": []}
            for ctype in ("must_include", "must_not_include", "bonus"):
                for idx, crit in enumerate(q.get(ctype, [])):
                    tasks.append((model, qid, ans["text"], f"{ctype}|{idx}|{crit}"))

    if not tasks:
        print("nothing to judge")
    else:
        print(f"Pending: {len(tasks)} judge calls")

    def write(model: str) -> None:
        out_paths[model].write_text(
            json.dumps(
                {"model": model, "judge": judge_model, "judgments": judgments_by_model[model]},
                indent=2,
            )
        )

    # initial flush so empty-answer cases are saved
    judgments_dir.mkdir(parents=True, exist_ok=True)
    for m in out_paths:
        with locks[m]:
            write(m)

    def judge_one(model: str, qid: str, answer_text: str, key: str) -> tuple[str, str, str, dict]:
        ctype, _idx, crit = key.split("|", 2)
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
        return model, qid, key, res

    done = 0
    total = len(tasks)
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(judge_one, m, qid, txt, key) for m, qid, txt, key in tasks]
        for fut in as_completed(futures):
            model, qid, key, res = fut.result()
            ctype, _idx, _crit = key.split("|", 2)
            with locks[model]:
                judgments_by_model[model][qid][ctype].append(res)
            done += 1
            if done % 25 == 0 or done == total:
                # flush all files periodically
                for m in out_paths:
                    with locks[m]:
                        write(m)
                print(f"  [{done}/{total}] {time.time() - start:.0f}s elapsed", flush=True)

    # finalize: compute scores per question, write
    for model, judgments in judgments_by_model.items():
        for j in judgments.values():
            if "score" in j:
                continue
            j["score"] = score_question(j)
        with locks[model]:
            write(model)
        print(f"  -> {out_paths[model]}")


def _size_hint(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]", model)
    return float(m.group(1)) if m else 0.0


def cmd_report(args: argparse.Namespace) -> None:
    qpath = Path(args.questions) if args.questions else QUESTIONS_PATH
    qdata = load_questions(qpath)
    questions = {q["id"]: q for q in qdata["questions"]}
    categories = sorted({q["category"] for q in qdata["questions"]})
    _, judgments_dir = _result_dirs(args)
    out_dir = judgments_dir.parent

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
    out_lines.append("| Model | Composite | Correctness | Safety viol. | Bonus | Q failed |")
    out_lines.append("|---|---:|---:|---:|---:|---:|")
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
    for m, comp, corr, viols, bonus, failed, _ in summary:
        out_lines.append(f"| `{m}` | {comp:+.2f} | {corr:.0%} | {viols} | {bonus:.0%} | {failed} |")
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
            cells.append(str(viols))
        out_lines.append("| " + " | ".join(cells) + " |")
    out_lines.append("")

    # --- Safety violations detail ---
    out_lines.append("## Safety Violations Detail\n")
    any_viol = False
    for m, *_, r in summary:
        per_model = []
        for qid, j in r["judgments"].items():
            for crit in j.get("must_not_include", []):
                if crit["verdict"] == "YES":
                    per_model.append((qid, crit["criterion"], crit["reason"]))
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

    out_lines.append("---\n")
    out_lines.append(
        "_Note on judge bias: when the judge is one of the evaluated models, its own answers "
        "may be over-rated. Categories with safety violations are the most reliable signal — "
        "they are objective rule violations rather than judgment calls._\n"
    )

    report_path = out_dir / "report.md"
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
    sp.set_defaults(func=cmd_all)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
