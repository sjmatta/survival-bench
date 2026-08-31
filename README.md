# survival-bench

A rubric-graded benchmark of survival, wilderness-medicine, and homestead
knowledge for any OpenAI-compatible LLM endpoint
(OpenRouter, LM Studio, vLLM, Ollama, …).

The runtime is **stdlib-only Python** — no install needed to run the bench
itself. Dev tooling (ruff, pytest, pre-commit, poethepoet) is opt-in.

## What's in the box

Three parallel benches, each with the same rubric structure:

| File | Shape | Questions | Notes |
|---|---|---:|---|
| `bench.json` (built) | text | 45 | wilderness + homestead-medical + calibration |
| `vision_questions.json` | image | 12 | paired edible/toxic-lookalike (mushroom, plant, snake) |
| `audio_questions.json` | audio | 7 | rattlesnake, canids, alarm-calls, thunder, owl |

Each question has three rubric types:

- **`must_include`** — required correct points (+1 each)
- **`must_not_include`** — safety-critical errors (−2 each, weighted heavily)
- **`bonus`** — depth and nuance (+0.5 each)

A judge model evaluates each criterion independently as YES/NO.
Per-criterion binary judging surfaces *why* a model failed and is more
reliable than free-form scoring.

The text bench in particular weighs **calibration** heavily: ~16 of the 45
questions are designed to catch confident fabrication when context is
missing. The single most-discriminating question across model classes is
`calib_11_fake_squash` — a made-up cultivar name that nearly all models
confidently classify rather than admit they don't recognize it.

## Setup

```bash
git clone git@github.com:sjmatta/survival-bench.git
cd survival-bench

# Configure your endpoint (OpenRouter or local)
cp .env.example .env
# edit .env — drop in OPENAI_API_KEY (and OPENAI_BASE_URL if not OpenRouter)

# Optional: dev tooling for tests + linting + pre-commit hooks
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install              # commit-time hooks (ruff, secrets, json)
pre-commit install --hook-type pre-push   # pytest before push
```

If you're pointing at a local LM Studio, `OPENAI_API_KEY` can be empty:

```env
OPENAI_BASE_URL=http://localhost:1234/v1
OPENAI_API_KEY=
```

## Running benchmarks

The main runner is `bench.py`, with three sub-commands: `generate`, `judge`,
`report` (and `all` to chain them). Tasks are wired through
[`poethepoet`](https://poethepoet.natn.io) for convenience.

```bash
# build the unified text bench from its source files
poe build                    # → bench.json (40+ questions)

# list models exposed by the configured endpoint
poe list-models

# full text run (generate → judge → report)
poe bench-text

# vision bench
poe resolve-images           # one-time: populate image URLs from Wikipedia
poe bench-vision

# audio bench
poe resolve-audio            # one-time: download Wikimedia + xeno-canto clips
poe bench-audio
```

Without poe, run the script directly:

```bash
python bench.py generate --questions bench.json --out-dir results --resume \
  --models "qwen/qwen3.6-27b,google/gemma-4-31b-it"
python bench.py judge   --questions bench.json --out-dir results --resume \
  --judge-model "anthropic/claude-sonnet-4.6"
python bench.py report  --questions bench.json --out-dir results
```

`generate` and `all` accept `--reasoning-effort` values `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, and `max`. Omitting the option sends no
reasoning configuration. Use `none` when a reasoning model otherwise consumes
the output budget before producing its final answer.

Run output lives in `results*/`:

- `results*/answers/<model>.json` — raw model responses
- `results*/judgments/<model>.json` — per-criterion judge verdicts
- `results*/report.md` — final markdown report

All result directories are gitignored; you commit your bench, not your runs.

### Audio bench notes

Audio clips are CC-licensed source material from Wikimedia Commons and
xeno-canto. **They are not committed to this repo** — `poe resolve-audio`
fetches them on demand to `audio_clips/`. You'll need:

- `ffmpeg` on your `PATH` (for trimming + re-encoding)
- An [xeno-canto API key](https://xeno-canto.org/account) for the questions
  that reference `xc_id` recordings. Add `XENOCANTO_API_KEY=...` to `.env`.

The runner trims each clip to ≤20 s mono 64 kbps mp3 — keeps them under
OpenRouter's audio-input size limit and within reasonable token cost.

### Adding questions

Source files: `questions.json` (wilderness), `calibration_questions.json`,
`vision_questions.json`, `audio_questions.json`. The text bench is assembled
from `questions.json` + `calibration_questions.json` + inline homestead
additions in `build_bench.py`. Edit, then `poe build`.

Each question entry needs:

```json
{
  "id": "category_NN_short",
  "category": "...",
  "prompt": "the user-facing scenario",
  "must_include": ["criterion phrased as the model behavior expected"],
  "must_not_include": ["criterion phrased as the violation behavior to flag"],
  "bonus": ["nice-to-have detail"]
}
```

Phrase `must_not_include` as the *violation itself* ("recommend X dangerous
thing"), not as the desired safe behavior. The judge is asked whether the
violation is present in the response.

For vision questions, add `images: [{"wiki_page": "Cantharellus_cibarius"}]`;
for audio, add `audio: [{"wiki_file": "File:..."}]` or
`audio: [{"xc_id": "1077982"}]`. Then run the matching `poe resolve-*` task.

## Results

Headline standings, judged by `claude-sonnet-4.6`. Composite = correctness +
0.25·bonus − 0.5·violations; numbers are illustrative. Hosted probes are kept
separate from **current-gen open-weight models that fit a 36 GB laptop** — the
off-grid conceit. Frontier models, local-hardware experiments, configurations,
and full methodology are in **[RESULTS.md](RESULTS.md)**.

**Recent hosted text probes** (OpenRouter, 45 questions, 1,024-token cap):

| Model | Composite | Correctness | Safety viol. | Bonus |
|---|---:|---:|---:|---:|
| `moonshotai/kimi-k3` | **+0.96** | 83% | 1 | 57% |
| `deepseek/deepseek-v4-pro-0813` | +0.77 | 73% | 5 | 40% |

Kimi nearly matched the closed-model ceiling but produced two degenerate
cap-length responses. DeepSeek needed reasoning disabled to return complete
final-answer text reliably and still failed five safety/calibration criteria.

A six-case local smoke test of
`huihui-ai/Huihui-Qwen3.8-27B-abliterated` (Q6_K) was stopped before a full run.
It hallucinated the fake squash cultivar and improvised dangerous snakebite and
pressure-canning instructions, so it has no comparable aggregate score.

**Laptop-sized text models** (45 questions):

| Model | Composite | Correctness | Safety viol. |
|---|---:|---:|---:|
| `qwen/qwen3.6-27b` | **+0.79** | 74% | 4 |
| `qwen/qwen3.6-35b-a3b` | +0.77 | 72% | 4 |
| `google/gemma-4-31b-it` | +0.74 | 69% | 3 |
| `google/gemma-4-26b-a4b-it` | +0.68 | 66% | 5 |
| `google/gemma-4-12b-it` † | +0.49 | 56% | 11 |
| `google/gemma-3n-e4b-it` | +0.46 | 52% | 11 |

**Vision** (12 questions):

| Model | Composite | Correctness | Safety viol. |
|---|---:|---:|---:|
| `qwen/qwen3.6-27b` | **+0.84** | 74% | 0 |
| `qwen/qwen3.6-35b-a3b` | +0.77 | 74% | 1 |
| `google/gemma-4-26b-a4b-it` | +0.58 | 58% | 1 |
| `google/gemma-4-31b-it` | +0.53 | 51% | 0 |
| `google/gemma-4-12b-it` † | +0.28 | 42% | 4 |

**Audio** (7 questions — small set, judge-fragile; treat as indicative):

| Model | Composite | Correctness | Safety viol. |
|---|---:|---:|---:|
| `xiaomi/mimo-v2.5` | **+0.54** | 51% | 0 |
| `google/gemini-3.1-pro-preview` | +0.45 | 49% | 1 |
| `mistralai/voxtral-small-24b-2507` | +0.43 | 40% | 0 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | +0.42 | 42% | 0 |
| `openai/gpt-audio-mini` | +0.38 | 36% | 0 |
| `google/gemma-4-12b-it` †‡ | +0.37 | 32% | 0 |
| `openai/gpt-audio` | +0.35 | 35% | 0 |

> † Local Q8_0 on an M4 Max — the only row run on the off-grid hardware itself
> (others are cloud fp8/fp16 proxies; Q8 ≈ fp16). ‡ Speech-only audio encoder:
> accurate ASR but misreads environmental sounds, hence the field's lowest audio
> correctness (32%). Full writeup in [RESULTS.md](RESULTS.md).

**The short version:** Kimi K3 is the strongest hosted non-frontier text model
tested (+0.96), within 0.01 of Claude Opus, but its token loops need a production
safeguard. Open-weight `qwen3.6-27b` remains the strongest local pick and beats
GPT-5.5 on vision (+0.84 vs +0.74). DeepSeek V4 Pro 0813 does not improve on it:
the larger hosted model scored slightly lower and made more safety-critical
calibration errors. This bench rewards knowing when *not* to invent specifics,
not raw scale or low refusal rates.

## Design choices

**Calibration over recall.** In a no-internet scenario, confident fabrication
is the dominant failure mode. The bench rewards "useful framework + honest
hedging" over both confident-wrong answers and reflexive refusal-to-engage.

**Region-invariance, mostly.** Identification is identification; once you've
correctly ID'd a death cap, the safe response doesn't change because you
crossed a state line. The bench is largely region-neutral by design;
`firstaid_03_snakebite` ("western US") is a scenario anchor, not a regional
test. The one missing-context calibration question (`calib_16_snakebite_unclear`)
tests whether the model asks for species/symptoms before branching.

**No copyrighted media in the repo.** Question files reference Wikipedia /
Wikimedia / xeno-canto sources by stable ID. Setup downloads them locally on
demand. None of the source media is redistributed here.

**Judge bias.** When the judge is one of the evaluated models, its own
answers may be over-rated. The `must_not_include` count is the most objective
signal — these are rule violations, not judgment calls. Run with two judges
and compare if you suspect bias.

## Development

```bash
poe lint          # ruff check
poe format        # ruff format
poe test          # pytest -q
poe check         # lint + tests
```

Pre-commit installs the same hooks plus a secret-scanner. `pre-push` runs
the test suite before letting you push.

## License

[MIT](LICENSE).
