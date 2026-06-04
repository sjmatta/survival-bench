# survival-bench — full results & methodology

The [README](README.md) carries the headline standings. This document has the
methodology, per-model analysis, the closed-source frontier baseline, and the
local-hardware experiments (quantization, abliteration, speculative decoding,
and the local Gemma 4 12B run).

All numbers come from a single multi-provider run and are illustrative —
re-running on your own endpoint produces fresh numbers.

## Judge migration & the flash-lite caveat

The judge evolved over the project: `claude-haiku-4.5` →
`gemini-2.5-flash-lite` (forced by an OpenRouter credit hold) →
`claude-sonnet-4.6` (current). The `gemini-2.5-flash-lite` judge proved **badly
miscalibrated on `must_not_include`**: it fired a "violation" whenever a
response *mentioned* a prohibited concept, even when the model was correctly
cautioning against it — inflating violation counts ~10× and distorting
composites. Re-judging with `claude-sonnet-4.6` collapsed those false positives
(text **915 → 75**, audio **66 → 1**, vision **190 → 12**). Relative ranking is
mostly preserved where scores are well-separated (text, vision); the
tightly-packed audio bench reshuffles. Sonnet is also stricter on
`must_include`, so absolute correctness and composites run *lower* than the old
flash-lite tables.

The headline **text / vision / audio / frontier** benches use Sonnet. The
**local-hardware experiment sections below** (quant, abliteration, finetune)
were **not** re-judged — they keep `gemini-2.5-flash-lite` held constant across
all rows so the within-experiment deltas stay clean. That's why they show 50–90
violations where the Sonnet benches show single digits: same answers, different
judge. **Compare within a section, never across judges**, and don't read the
flash-lite violation counts as absolute danger counts.

## Text bench — per-model detail

Headline standings are in the [README](README.md#results). Under the
`claude-sonnet-4.6` judge (OpenRouter-hosted, fp8, reasoning off):

- `qwen/qwen3.6-27b` — best open-weight current-gen, narrowly on top at +0.79.
- `qwen/qwen3.6-35b-a3b` — within 0.02 composite; better on community_medical questions, slightly weaker on first_aid.
- `google/gemma-4-31b-it` and `gemma-4-26b-a4b-it` — close behind the Qwens (+0.74 / +0.68), a much tighter field than the flash-lite run implied. The earlier wide gap was mostly the old judge over-firing on the Gemmas' terser answers; with that noise removed they're competitive.
- `google/gemma-3n-e4b-it` — last (+0.46) and most violation-prone (11). The smallest model is consistently the most likely to fabricate confidently when context is missing.

## Local quant + uncensored finetune

Three-way comparison isolating thinking mode, quantization, and finetune.
All rows judged by `gemini-2.5-flash-lite` (held constant — see the caveat
above; not comparable to the Sonnet headline, where the same `qwen3.6-27b` fp8
baseline scores +0.79).

| Model | Quant | Reasoning | Composite | Correctness | Safety viol. | Bonus | Q failed |
|---|---|---|---:|---:|---:|---:|---:|
| `qwen/qwen3.6-27b` (OpenRouter) | fp8 | off | **+0.42** | 91% | 56 | 53% | 10 |
| `qwen/qwen3.6-27b` (OpenRouter) | fp8 | medium | +0.32 | 91% | 67 | 52% | 13 |
| `qwen3.6-27b-heretic-uncensored` (LM Studio) | Q4_K_M | on | +0.28 | **93%** | 73 | **58%** | 17 |

**Takeaways:**

- **Thinking-on alone drops composite by ~0.11** on the baseline (+0.42 →
  +0.32) while leaving correctness identical. The reasoning mode generates
  longer, more defensive answers, and the Gemini judge fires positive
  whenever a response *mentions* a prohibited concept — even when the model
  is correctly cautioning against it. So reasoning-on accumulates +11 judge
  "violations" that are mostly artifacts. (E.g. on `water_01_pond` the
  judge's own reason text says *"The response explicitly states that
  boiling does NOT remove algal toxins"* — and then flags it as a violation
  for "claiming boiling removes chemical contamination.")
- **The heretic vs. thinking-on baseline gap is small** (+0.32 → +0.28).
  Most of the originally-shocking −0.14 composite gap between heretic and
  the headline baseline was **thinking-mode artifact**, not the finetune.
  Apples-to-apples (both thinking-on), the residual gap is only −0.04 — and
  the heretic is +2pp on correctness and +6pp on bonus, with more
  violations. A wash, mostly noise.
- **The de-censoring + Q4_K_M quant did not measurably break the model on
  survival knowledge.** On the bench's famed `calib_11_fake_squash` (the
  made-up cultivar trap most models fail), the heretic scored *higher*
  correctness than baseline (100% vs 67%).
- **Practical implication for hobby/off-grid laptop setups**: a community
  Q4_K_M GGUF on LM Studio is essentially equivalent to OpenRouter fp8 on
  this bench, once you control for reasoning mode. The cost is one ~2-hour
  bench run on local hardware vs. seconds on a cloud endpoint.

### Aside: speculative decoding (MTP) on this hardware tier

Tested Google's official MTP drafter (`google/gemma-4-26B-A4B-it-assistant`,
April 2026 release) against the base 26B-A4B target via AtomicChat's
[`atomic-llama-cpp-turboquant`](https://github.com/AtomicBot-ai/atomic-llama-cpp-turboquant)
fork on M4 Max. Result: **76.4 tok/s → 78.3 tok/s (+2.5%)** at Q4_K_M, with
a healthy 76% draft acceptance rate (258/338). Google's "3×" headline number
is for BF16 targets where per-token verify is much slower; on a Q4 MoE with
only ~4B active params per token, draft+verify overhead nearly cancels the
savings. Aligns with [April's RTX 3090
benchmark](https://github.com/thc1006/qwen3.6-speculative-decoding-rtx3090)
showing no net speedup for classic spec-decoding on MoE targets. For local
laptop inference at Q4, MTP isn't a meaningful upgrade today — revisit when
better Q4-MTP kernels land.

## Abliterated vs base — does removing the refusal direction hurt knowledge?

The [heretic finetune above](#local-quant--uncensored-finetune) was a
roleplay-targeted model and not a clean test of de-censoring alone. To
isolate the effect of refusal removal, this section benches each model
against its [huihui-ai abliterated](https://huggingface.co/huihui-ai)
counterpart — same architecture, same training data, the only difference
is orthogonal projection of the "refusal direction" out of the
post-attention residual stream. No new training, no roleplay objective.

All Q4 runs below were on the same M4 Max via `llama-server` from the
[AtomicChat turboquant fork](https://github.com/AtomicBot-ai/atomic-llama-cpp-turboquant),
Q4_K_M, thinking-off, judged by `gemini-2.5-flash-lite` (held constant across
all rows so the quant cliff is clean). The fp8/fp16 cloud rows are the same
OpenRouter models as the headline run, re-judged here under flash-lite for
apples-to-apples with the local Q4 rows — so they read lower than the
Sonnet-judged headline. Compare within this section.

| Model | Quant / Runtime | Composite | Correctness | Safety viol. | Bonus |
|---|---|---:|---:|---:|---:|
| `qwen/qwen3.6-27b` base | fp8 OpenRouter | **+0.42** | 91% | 56 | 53% |
| `qwen3.6-27b` abliterated | Q4 local | +0.28 | 90% | 70 | **58%** |
| `qwen3.6-27b` base | Q4 local | +0.16 | **92%** | 83 | 56% |
| `gemma-4-26b-a4b-it` base | fp16 OpenRouter | +0.28 | 86% | 63 | 47% |
| `gemma4-26b-a4b` base | Q4 local | +0.09 | 90% | 86 | 47% |
| `gemma4-26b-a4b` abliterated | Q4 local | −0.05 | 81% | 88 | 40% |

**Takeaways:**

- **Abliteration's effect is model-dependent — opposite signs.** Clean
  Q4-vs-Q4, same runtime, only difference is the refusal-direction
  projection:
  - **Qwen 3.6: abliteration *helped* (+0.12 composite).** Abliterated
    +0.28 vs base +0.16. Correctness is statistically flat (90% vs 92%),
    bonus flat (58% vs 56%); the gain is entirely from *fewer*
    judge-flagged safety violations (70 vs 83). That's almost certainly
    a judge artifact, not the abliterated model being genuinely safer —
    the Gemini judge over-fires on responses that *mention* a prohibited
    concept even while cautioning against it, and the abliterated qwen's
    terser, less-hedgy phrasing trips that false-positive less often.
    Net: abliterating qwen 3.6 did **not** damage survival knowledge.
  - **Gemma 4: abliteration *hurt* (−0.14 composite).** Abliterated
    −0.05 vs base +0.09. Correctness drops 9pp (90% → 81%), bonus drops
    7pp (47% → 40%), violations roughly flat (86 → 88). Same intervention,
    same bench — Gemma's knowledge measurably degraded; qwen's didn't.
    Recipe and base model both matter; "abliteration is harmless" does
    not generalize.
- **The Q4 quant cliff is the single biggest factor for both models,
  and it's brutal for qwen.** Base qwen 3.6 27B: +0.42 at fp8 cloud →
  **+0.16** at Q4 local, a −0.26 composite drop. Base gemma 4: +0.28
  fp16 → +0.09 Q4, −0.19. Dense 27B qwen loses more to Q4 than the
  sparse 4B-active gemma MoE does. The earlier "−0.14 abliterated-qwen
  gap" was **entirely the quant cliff** — at equal quant the abliterated
  qwen actually *beats* base qwen.
- **Practical implication for off-grid laptop use**: at Q4 on this
  hardware, **abliterated qwen 3.6 27B (+0.28) is the strongest local
  pick** — it both refuses less *and* outscores every other Q4 local
  variant here, including its own base. Abliterated Gemma 4 is the one
  to avoid: you pay a real correctness tax on top of the quant tax for
  the de-censoring.

## Vision bench — detail

Headline standings are in the [README](README.md#results).
`gemma-3n-e4b-it` is text-only on OpenRouter and excluded.

Both Qwens lead, with `qwen3.6-27b` now on top (the two flipped vs. the
flash-lite run). The Gemmas land mid-pack and **positive** (+0.58 / +0.53) —
their earlier negative scores were almost entirely flash-lite over-firing on
terse answers (24–25 spurious violations each, now ~0), not real failures. They
still trail on mushroom ID, where their terser answers (200–350 tokens vs.
Qwen's 1400–2900) cost them on `must_include` items expecting feature-citation.

## Audio bench — detail

> **Correction (re-run with audio actually attached).** Earlier versions of this
> table were invalid: the harness was sending the *text prompt only* — the clips
> were never resolved to `local_path`, so every model answered blind. The numbers
> are now from runs where the audio is genuinely attached, and the OpenAI
> `gpt-audio*` models (which *require* audio input and 400'd on text-only
> requests) are scoreable.

Headline standings are in the [README](README.md#results). The open-weight omni
model `xiaomi/mimo-v2.5` leads (+0.54), with violations near zero across the
field once the flash-lite over-firing is gone.

> **This ranking is judge-fragile — read it as indicative, not precise.** With
> only 7 questions the composites cluster inside a 0.2 band (+0.35 to +0.54), so
> the judge choice dominates the order. Under `gemini-2.5-flash-lite`,
> `gpt-audio-mini` led and `gemini-3.1-pro` came *last*; under `claude-sonnet-4.6`,
> `mimo-v2.5` leads and `gemini-3.1-pro` is *second*. Don't over-read the order.

The `audio_signal` category (crow + chickadee alarm calls) remains the hardest —
the "what is the call signalling?" inference trips every model — and even with
audio attached, `gemini-3.1-pro` still misidentifies the wolf howl as coyotes
(an answer-level error independent of judge).

## Gemma 4 12B (local) — the audio-capable Gemma, on all three benches

Gemma 4's 26B-A4B and 31B variants are image/text/video only — no audio. The
**12B** is the exception: an "encoder-free" multimodal model that projects audio
(and vision) directly, making it the one Gemma 4 that can take sound. It isn't on
OpenRouter, so this is a **local run — Q8_0 GGUF on an M4 Max**, served via
`llama-server`. Three wrinkles worth recording: it needs a recent **mainline**
llama.cpp (the `gemma4uv` projector isn't in the AtomicChat fork — it errors with
`unknown projector type: gemma4uv`); the mmproj **must be BF16** (F16/Q8 degrade
the numerically-sensitive projector); and unlike the larger Gemmas the 12B is a
**reasoning model** by default. Judged by `claude-sonnet-4.6`, same as the
headline benches.

| Bench | Composite | Correctness | Safety viol. | Rank in field |
|---|---:|---:|---:|---|
| Text | +0.49 | 56% | 11 | 13 / 17 |
| Vision | +0.28 | 42% | 4 | 10 / 12 |
| Audio | +0.37 | **32%** | 0 | 6 / 7 |

**Takeaways:**

- **It underperforms its larger Gemma 4 siblings**, as expected for a smaller
  model at a local quant. On text it sits below cloud `gemma-4-31b` (+0.74) and
  `gemma-4-26b-a4b` (+0.68); on vision it's the weakest Gemma but still clears
  both Llamas. Its 11 text violations echo the reasoning-on-adds-violations
  pattern documented in the local-quant section.
- **Its audio is real but speech-only.** Direct probing settled it: a spoken
  sentence transcribes near-perfectly (accurate ASR), but environmental and
  animal sounds are out-of-distribution for its USM/Conformer *speech* encoder —
  it hears a rattlesnake rattle as "clapping," thunder as a "cricket." On the
  audio bench that surfaces as the **lowest correctness in the field (32%)**; the
  only reason its composite (+0.37) isn't last is that it still gives cautious,
  sensible safety guidance and commits zero violations. So the honest answer to
  "does Gemma 4 do audio?" is: **yes — but for *speech*, not the environmental-
  sound identification this bench tests.**

## Frontier baseline (closed-source, for context)

For comparison with the current-generation open-weight standings, we also ran
the closed-source frontier models direct against each provider's API (Anthropic,
Google, OpenAI). These do **not** fit the bench's hobby/off-grid scenario —
they're API-only — but they bound the achievable ceiling. Same judge
(`claude-sonnet-4.6`).

**Text bench:**

| Model | Composite | Correctness | Safety viol. | Bonus |
|---|---:|---:|---:|---:|
| `anthropic/claude-opus-4.7` | **+0.97** | 86% | 1 | 61% |
| `openai/gpt-5.5` | +0.88 | 79% | 2 | 41% |
| `google/gemini-3.1-pro-preview` | +0.81 | 76% | 4 | 39% |

**Vision bench:**

| Model | Composite | Correctness | Safety viol. | Bonus |
|---|---:|---:|---:|---:|
| `anthropic/claude-opus-4.7` | **+0.97** | 90% | 0 | 64% |
| `google/gemini-3.1-pro-preview` | +0.94 | 88% | 1 | 42% |
| `openai/gpt-5.5` | +0.74 | 74% | 0 | 32% |

**Audio bench** (Anthropic doesn't support audio). With audio properly attached
the closed frontier audio models cluster tightly and are **beaten by the
open-weight `xiaomi/mimo-v2.5`** (+0.54):

| Model | Composite | Correctness | Safety viol. |
|---|---:|---:|---:|
| `google/gemini-3.1-pro-preview` | **+0.45** | 49% | 1 |
| `openai/gpt-audio-mini` | +0.38 | 36% | 0 |
| `openai/gpt-audio` | +0.35 | 35% | 0 |

**Headline takeaways:**

- The open-weight `qwen3.6-27b` (text composite +0.79) sits ~0.09 below
  GPT-5.5 (+0.88) and ~0.18 below the Claude Opus ceiling (+0.97). The
  frontier leads, but not enormously; the bench is largely about *correct
  calibration on hard cases*, not raw capability.
- On the **vision bench**, the open-weight `qwen3.6-27b` (+0.84) **beats
  GPT-5.5** (+0.74) and trails only Claude Opus (+0.97) and Gemini 3.1 Pro
  (+0.94). Image-grounded reasoning is where the open-weight Qwen models
  genuinely shine.
- On the **audio bench**, the open-weight omni model `xiaomi/mimo-v2.5`
  leads (+0.54), ahead of every closed frontier audio model — though the
  field clusters inside a 0.2 band over just 7 questions, so the order is
  judge-fragile.
- All models — frontier *and* open-weight — fall into the same calibration
  traps. `calib_11_fake_squash` hits everyone roughly equally; correctness
  mostly tracks model capability.

## Notable patterns

- **The fake-cultivar question (`calib_11_fake_squash`) trips nearly every model.** 4 of 5 text models confidently classify the made-up name as *Cucurbita pepo* and predict cross-compatibility rather than admit they don't recognize the cultivar. Single highest-leverage calibration question in the bench — the test that survives across model families.
- **Smallest model is the most dangerous.** `gemma-3n-e4b-it` racks up the most safety violations of any text model (11 under the Sonnet judge), all confident fabrications. The pattern is consistent: it not only confirms false premises but invents supporting mechanisms (juglone-cyanide for acorns, specific protocols for untested combinations).
- **Drug interaction (`calib_04`) is universally weak.** Every model scored 0-42% correctness — the question deliberately omits which blood thinner and which antibiotic, and most models either refuse entirely or give generic "consult your doctor" advice rather than asking which two drugs (the right answer).
- **Ceiling-effect saturation in some questions** — `water_02_snow`, `calib_02_silver_test`, `calib_06_rash_diagnosis`, `calib_10_bleach_ammonia` all max out across models. These are intentional sanity checks. The bleach+ammonia question specifically tests *appropriate* confidence (right answer is a confident "no, makes chloramine gas"); over-hedging there is also a failure. Worth keeping in the bench despite zero discrimination.
- **Audio is the hardest modality** — and the most error-prone to *bench*. The original audio results were silently wrong because the clips were never attached (text-only requests); always confirm `local_path` is populated in the questions file before trusting audio scores. The "what is the call signalling?" inference layer (crow/chickadee alarm calls) trips every model, and even frontier `gemini-3.1-pro` misidentifies a wolf howl as coyotes. The open-weight omni model `xiaomi/mimo-v2.5` tops the bench, but the whole field clusters within ~0.2 composite over just 7 questions, so the audio standings are judge-sensitive (the flash-lite and Sonnet judges crown different winners) — treat them as indicative, not precise.
