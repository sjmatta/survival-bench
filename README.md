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
  --models "qwen/qwen3.8-27b" --reasoning-effort none --max-tokens 4000
python bench.py generate --questions bench.json --out-dir results --resume \
  --models "meta/muse-glimmer-30b" --max-tokens 8192
# After grading:
python bench.py report --questions bench.json --out-dir results
```

**Current judge setup:** The Opus 5 evaluation used a run-specific wrapper with
a larger judge budget and validation of final YES/NO output. The stock `judge`
command still caps output at 128 tokens and is not sufficient for reproducing
that run; see [configuration and verification](RESULTS.md#configuration-and-verification).

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

The active evaluation cohort starts September 6, 2026, with **Claude Opus 5** as
judge. The goal is useful offline knowledge on a **36 GB M4 Max laptop**; hosted
runs screen models whose quantized weights could plausibly run there.
Composite = correctness + 0.25·bonus − 0.5·violations per question, clipped at −1,
then averaged. Results are illustrative, from one answer per question.

**Latest laptop-sized comparison — September 6, 2026** (hosted and local generation,
`anthropic/claude-opus-5` judge):

| Model | Text composite | Text correctness | Text violations | Vision composite | Vision correctness | Vision violations |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-glimmer-30b` | **+0.85** | 78% | 4 | **+0.83** | 76% | 0 |
| `qwen/qwen3.8-27b` | +0.77 | 76% | 8 | +0.62 | 65% | 3 |
| K2 Horizon MoVA 36B-A4B, local Q4_K_M | +0.69 | 69% | 8 | — | — | — |
| Granite 4.2 30B, local Q4_K_M | +0.58 | 64% | 10 | — | — | — |
| `nvidia/nemotron-3.5-lightning` | +0.58 | 63% | 11 | — | — | — |
| `ibm-granite/granite-4.2-8b` | +0.50 | 59% | 15 | — | — | — |

Muse leads both benches in the current cohort. The four additions completed the
45-question text bench; dashes indicate unsupported vision input. Granite 30B
and Nemotron are effectively tied on composite (their unrounded scores differ
by less than 0.00003). The sharpest vision difference was the coral-snake photo:
Muse identified the venomous snake and warned against handling; Qwen called it
a harmless kingsnake and said it could be safely removed from the tent. Both
invented knowledge about the fake squash cultivar. Counts are triggered
`must_not_include` criteria, including refusal/calibration penalties, not counts
of distinct dangerous answers.

K2 and Granite 30B are measured local Q4_K_M runs on this laptop, using low
reasoning effort and a 4,096-token cap. The other rows are hosted proxies for
locally feasible models. Qwen used thinking off and a 4,000-token cap; Muse used
default reasoning and an 8,192-token cap. Muse and Qwen completed 45 text and
12 vision answers each. All four additions completed 45 text answers each.
Full configuration, judge-budget repair, and review caveats are in
[RESULTS.md](RESULTS.md).

Previous text, vision, audio, frontier, and local-hardware results are preserved
in **[ARCHIVE.md](ARCHIVE.md)**. They are historical evidence, not an active rerun
queue. Detailed current results are in **[RESULTS.md](RESULTS.md)**.

### Next evaluations

The four requested additions are complete. Remaining optional work:

| Candidate | What it adds | Access |
|---|---|---|
| LFM2.5-2.6B / VL-3B | Tiny text and vision models for a low-memory tier | Text on OpenRouter; vision needs another route |
| MOSS Audio 4B Thinking + Audio Flamingo Next | Environmental-sound understanding for the seven-question audio bench | Local setup; Mac runtime validation needed |
| Gemini 3.8 Flash | Fresh hosted audio reference | OpenRouter; not a laptop-weight candidate |

[Candidate sources and local-fit qualifications](RESULTS.md#candidate-sources-and-remaining-evaluations)
are maintained with the detailed results. Audio has no active-cohort score yet;
the current two headline models accept no audio. The separate
[audio research shortlist](AUDIO_CANDIDATES.md) explains the recommendations,
including the limitations of the MOSS 8B community port. A local quantized Muse
run and a thinking-enabled Qwen3.8 run would answer additional configuration
questions. Archived models do not need to be rerun to keep this cohort current.

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
answers may be over-rated. The `must_not_include` count makes individual
failures inspectable, but
verdicts still depend on interpretation. Review the underlying answers and
criteria when a flag is ambiguous.

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
