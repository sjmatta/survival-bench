# survival-bench — results & methodology

The [README](README.md#results) has the headline standings. This is the full
record: the judge story, per-bench detail, the frontier ceiling, and the
local-hardware experiments. All numbers are from one multi-provider run and are
illustrative — re-running on your endpoint produces fresh ones.

## The judge — and why the numbers shifted

Judge history: `claude-haiku-4.5` → `gemini-2.5-flash-lite` (forced by a credit
hold) → **`claude-sonnet-4.6`** (current). Flash-lite was badly miscalibrated on
`must_not_include`: it flagged a violation whenever a response *mentioned* a
prohibited concept, even while correctly cautioning against it — inflating
violations ~10×. Re-judging with Sonnet collapsed the false positives:

| Bench | Violations (flash-lite → Sonnet) |
|---|---|
| Text | 915 → 75 |
| Vision | 190 → 12 |
| Audio | 66 → 1 |

Ranking is stable where scores are well-separated (text, vision) and reshuffles
on the tight audio bench. Sonnet is also stricter on `must_include`, so its
composites run lower — **not comparable to old flash-lite tables**.

The headline benches use Sonnet. The **local-hardware sections** below keep
flash-lite held constant across their rows so within-experiment deltas stay clean
— that's why they show 50–90 violations. **Compare within a section, never across
judges.**

## Per-bench detail

Headline tables are in the [README](README.md#results).

**Text** (Sonnet, fp8, reasoning off):

- `qwen3.6-27b` leads (+0.79); `qwen3.6-35b-a3b` within 0.02.
- `gemma-4-31b` / `-26b-a4b` close behind (+0.74 / +0.68) — the flash-lite run's wide gap was mostly over-firing on their terser answers.
- `gemma-3n-e4b` last (+0.46), most violations (11): the smallest model fabricates most when context is missing.

**Vision:**

- Both Qwens lead; `qwen3.6-27b` on top (+0.84, flipped vs. flash-lite). `gemma-3n` is text-only here and excluded.
- The Gemmas are mid-pack and **positive** (+0.58 / +0.53) — their old negative scores were ~24 spurious flash-lite violations each, not real failures. They still trail on mushroom ID, where terse answers (200–350 tok vs. Qwen's 1400–2900) miss `must_include` feature-citations.

**Audio** (7 questions — small and judge-fragile):

- `xiaomi/mimo-v2.5` leads (+0.54); violations near zero across the field under Sonnet.
- **Read the order as indicative, not precise.** Composites cluster in a 0.2 band, so the judge dominates: flash-lite crowned `gpt-audio-mini` with `gemini-3.1-pro` *last*; Sonnet crowns `mimo-v2.5` with `gemini-3.1-pro` *second*.
- The `audio_signal` category (alarm calls) is hardest — the "what is the call signalling?" inference trips everyone, and `gemini-3.1-pro` even misreads the wolf howl as coyotes.
- **Earlier audio runs were invalid:** clips weren't attached (the harness sent text-only prompts), so models answered blind and OpenAI's `gpt-audio*` 400'd. Always confirm `local_path` is populated before trusting audio scores.

## Frontier ceiling (closed-source, for context)

Closed models run direct against each provider's API. They don't fit the off-grid
scenario but bound the ceiling. Same Sonnet judge.

**Text:**

| Model | Composite | Correct | Viol. | Bonus |
|---|---:|---:|---:|---:|
| `anthropic/claude-opus-4.7` | **+0.97** | 86% | 1 | 61% |
| `openai/gpt-5.5` | +0.88 | 79% | 2 | 41% |
| `google/gemini-3.1-pro-preview` | +0.81 | 76% | 4 | 39% |

**Vision:**

| Model | Composite | Correct | Viol. | Bonus |
|---|---:|---:|---:|---:|
| `anthropic/claude-opus-4.7` | **+0.97** | 90% | 0 | 64% |
| `google/gemini-3.1-pro-preview` | +0.94 | 88% | 1 | 42% |
| `openai/gpt-5.5` | +0.74 | 74% | 0 | 32% |

**Audio** (Anthropic has no audio):

| Model | Composite | Correct | Viol. |
|---|---:|---:|---:|
| `google/gemini-3.1-pro-preview` | **+0.45** | 49% | 1 |
| `openai/gpt-audio-mini` | +0.38 | 36% | 0 |
| `openai/gpt-audio` | +0.35 | 35% | 0 |

The gap is modest: open-weight `qwen3.6-27b` is ~0.09 below GPT-5.5 on text and
**beats** it on vision (+0.84 vs +0.74), trailing only Opus and Gemini; on audio
the open-weight `mimo-v2.5` (+0.54) tops every closed model. The bench rewards
calibration on hard cases, not raw scale.

## Running it off-grid (local hardware)

Local runs are on an M4 Max via `llama-server` (AtomicChat
[turboquant fork](https://github.com/AtomicBot-ai/atomic-llama-cpp-turboquant)),
flash-lite judge held constant per the experiment.

### Gemma 4 12B — the audio-capable Gemma

Gemma 4's 26B-A4B and 31B are image/text/video only. The **12B** is the exception
— an "encoder-free" model that projects audio and vision directly, so it's the
one Gemma 4 that takes sound. Not on OpenRouter; run here as a Q8_0 GGUF,
Sonnet-judged (it's in the [README](README.md#results) standings).

| Bench | Composite | Correct | Rank |
|---|---:|---:|---|
| Text | +0.49 | 56% | 13/17 |
| Vision | +0.28 | 42% | 10/12 |
| Audio | +0.37 | 32% | 6/7 |

- **Underperforms its larger siblings** — expected for a smaller model: below cloud `gemma-4-31b` / `-26b-a4b` on text, weakest Gemma on vision (still beats both Llamas). Its 11 text violations track the reasoning-on pattern below (the 12B reasons by default).
- **Audio is real but speech-only.** A spoken sentence transcribes near-perfectly, but environmental sounds are out-of-distribution for its USM/Conformer *speech* encoder — it hears a rattle as "clapping," thunder as a "cricket." Hence the field's lowest audio correctness (32%); the +0.37 composite survives only on cautious safety advice. So Gemma 4 *does* do audio — for **speech**, not the environmental ID this bench tests.
- **Serving gotchas:** needs **mainline** llama.cpp (the AtomicChat fork rejects the `gemma4uv` projector); the mmproj **must be BF16** (F16/Q8 degrade it).

### Quantization & the Q4 cliff

| `qwen3.6-27b` variant | Quant | Reasoning | Composite | Correct | Viol. | Bonus |
|---|---|---|---:|---:|---:|---:|
| base | fp8 | off | **+0.42** | 91% | 56 | 53% |
| base | fp8 | medium | +0.32 | 91% | 67 | 52% |
| base | Q4 local | off | +0.16 | 92% | 83 | 56% |
| heretic-uncensored | Q4_K_M | on | +0.28 | 93% | 73 | 58% |

- **Q4 is the biggest single factor.** Base 27B drops +0.42 (fp8) → **+0.16** (Q4), a −0.26 cliff. Dense 27B loses more to Q4 than gemma's sparse 4B-active MoE (+0.28 → +0.09).
- **Thinking-on costs ~0.11** (+0.42 → +0.32) at flat correctness — longer answers just trip more flash-lite false-positives.
- **The heretic finetune is a wash** vs. the thinking-on baseline; most of the original −0.14 "heretic gap" was thinking-mode artifact, not de-censoring.

### Abliteration (refusal-direction removal)

[huihui-ai abliterated](https://huggingface.co/huihui-ai) counterparts — same
weights, refusal direction projected out, no retraining. Q4 local throughout.

| Model | Composite | Correct | Viol. | Bonus |
|---|---:|---:|---:|---:|
| `qwen3.6-27b` abliterated | **+0.28** | 90% | 70 | 58% |
| `qwen3.6-27b` base | +0.16 | 92% | 83 | 56% |
| `gemma4-26b-a4b` base | +0.09 | 90% | 86 | 47% |
| `gemma4-26b-a4b` abliterated | −0.05 | 81% | 88 | 40% |

- **Effect is model-dependent, opposite signs.** Abliteration *helped* qwen (+0.12, entirely from fewer flash-lite false-positives on terser phrasing — not genuinely safer) and *hurt* gemma (−0.14: correctness −9pp, bonus −7pp — real knowledge damage). "Abliteration is harmless" does not generalize.
- **Best local pick: abliterated qwen3.6-27B (+0.28)** — refuses less *and* outscores every other Q4 local variant, including its own base. Avoid abliterated Gemma 4.

### Speculative decoding (MTP)

Google's official MTP drafter (`gemma-4-26B-A4B-it-assistant`) vs. base 26B-A4B,
Q4_K_M: **76.4 → 78.3 tok/s (+2.5%)**, 76% draft acceptance. Google's "3×" is for
BF16 targets; on a Q4 MoE with ~4B active params, draft+verify overhead nearly
cancels the gain (matches [RTX 3090 results](https://github.com/thc1006/qwen3.6-speculative-decoding-rtx3090)).
Not worth it at Q4 today.

## Calibration patterns

- **`calib_11_fake_squash` (made-up cultivar) trips nearly every model** — most confidently classify it as *Cucurbita pepo* rather than admit they don't know it. The single most discriminating question in the bench.
- **Smallest = most dangerous:** `gemma-3n-e4b` leads text violations (11), inventing supporting mechanisms (juglone-cyanide for acorns, protocols for untested drug combinations).
- **`calib_04` (drug interaction) is universally weak** (0–42% correct): it omits which two drugs, and models give generic "consult your doctor" instead of asking which ones.
- **Saturated sanity checks** — `water_02_snow`, `calib_02_silver_test`, `calib_06_rash_diagnosis`, `calib_10_bleach_ammonia` max out everywhere. Bleach+ammonia tests *appropriate* confidence (a firm "no, chloramine gas"); over-hedging there is also a failure.
