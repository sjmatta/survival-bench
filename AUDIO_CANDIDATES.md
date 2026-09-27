# Audio evaluation candidates — September 6–15, 2026

The first three-model batch has now been evaluated; see [detailed results](RESULTS.md#september-6-audio-evaluation). A separate [NatureLM-audio run](RESULTS.md#september-15-naturelm-audio-evaluation) completed on September 15. The existing
seven-question set covers rattlesnakes, canids, bird alarm calls, thunder, and an
owl. Environmental-sound understanding matters more here than transcription.
Archived audio results remain in [ARCHIVE.md](ARCHIVE.md).

## First batch and outcome

**Gemini 3.8 Flash scored +0.83 on 7/7 questions. Audio Flamingo Next scored
−0.02 on 7/7. MOSS 4B Thinking completed 6/7; its completed-only composite was
−0.17, with the wolf answer stuck in a thinking loop.** These results do not
support recommending either tested local configuration for this survival
workload. MOSS 8B remains an optional challenger, subject to its known non-speech
runtime caveats; it was not part of this batch.

**NatureLM-audio later completed 7/7 questions with composite −0.08, 13%
correctness, 0% bonus, and 3 violation flags.** It identified the wolf, owl,
crow alarm, and chickadee alarm coarsely, but misidentified the rattlesnake as
a Plains Bush Cricket and the coyote as a Great Horned Owl. See the
[detailed NatureLM run](RESULTS.md#september-15-naturelm-audio-evaluation).

| Candidate | Release | Input → output | Local feasibility / serving |
|---|---|---|---|
| `OpenMOSS-Team/MOSS-Audio-4B-Thinking` | April 13, 2026 | Audio + text → text | Approximately 4.6B total including encoder. Community MLX 4-bit port reports 3.8 GB transient peak on M3 Ultra; ample room on 36 GB, approximately 4 GB observed here, with 6/7 completed answers. Not in checked OpenRouter catalog. |
| `nvidia/audio-flamingo-next-hf` | April 13, 2026 | Audio + text → text | 8B total; approximately 16 GB BF16 weights before overhead. Official Transformers implementation; completed here on Metal with the original float64 timing operation on CPU. No checked OpenRouter/HF Inference Provider deployment. |
| `OpenMOSS-Team/MOSS-Audio-8B-Thinking` | April 13, 2026 | Audio + text → text | Approximately 8.6B total. Community hybrid MLX port reports 8.1 GB transient peak, but also non-speech degeneration; its author recommends 4B for ambient audio. Not in checked OpenRouter catalog. |
| `google/gemini-3.8-flash` | September 2, 2026 | Audio/text/images/video/files → text | OpenRouter available. Hosted ceiling only; no downloadable laptop weights. |
| `EarthSpeciesProject/NatureLM-audio` | February 27, 2025; checkpoint updated May 27, 2025 | Audio + text → text | Bioacoustics specialist built around Llama 3.1 8B, BEATs, and an audio Q-Former. Official local PyTorch API, not OpenRouter; requires gated Meta-Llama 3.1 8B access. Evaluated here on the 36 GB M4 Max: 7/7 complete, composite −0.08, 13% correctness, 3 violation flags. |

The MOSS family and Audio Flamingo Next explicitly cover speech, environmental
sounds, and music. Their published capabilities make them relevant candidates;
the first three-model evaluation is summarized above. Published conversion
footprints describe the authors' hardware unless explicitly marked as measured here.

Sources: [MOSS original repository and release](https://github.com/OpenMOSS/MOSS-Audio),
[MOSS 4B MLX runtime](https://huggingface.co/RumiLabs/MOSS-Audio-4B-Thinking-MLX-4bit),
[MOSS 8B MLX runtime and limitations](https://huggingface.co/RumiLabs/MOSS-Audio-8B-Thinking-MLX-hybrid),
[NVIDIA official checkpoint](https://huggingface.co/nvidia/audio-flamingo-next-hf),
[Audio Flamingo Next paper](https://arxiv.org/abs/2604.10905),
[Gemini OpenRouter endpoint](https://openrouter.ai/google/gemini-3.8-flash),
[NatureLM-audio repository](https://github.com/earthspecies/NatureLM-audio),
[NatureLM-audio checkpoint](https://huggingface.co/EarthSpeciesProject/NatureLM-audio),
[NatureLM-audio paper](https://openreview.net/forum?id=hJVdwBpWjt),
[model-merging update](https://arxiv.org/abs/2511.05171).

## Alternatives and exclusions

- `openbmb/MiniCPM-o-4_5` is a 9B omni model with an official Mac deployment path
  through llama.cpp-omni and quantized components. It is more oriented toward
  speech and interaction than these specialist sound models, so it is a lower
  priority. [Official card and deployment](https://huggingface.co/openbmb/MiniCPM-o-4_5).
- MOSS 8B Instruct is a fallback if Thinking decoding proves unusable. A community
  MLX port reports 7.85 GB peak on an M1 Max 32 GB, but that validates its speech
  path rather than animal-sound identification.
  [Instruct port](https://huggingface.co/fredchu/MOSS-Audio-8B-Instruct-MLX).
- Muse Spark 1.3 advertises audio input in the catalog, but its actual OpenRouter
  page warns that audio understanding is not fully supported and may be degraded.
  Do not use it as the ceiling yet.
  [Provider caveat](https://openrouter.ai/meta/muse-spark-1.3).
- ASR-only and TTS models do not answer the benchmark's non-speech identification
  questions. An audio-only captioner also needs a separate reasoning stage to
  answer arbitrary prompts, which would be a different system evaluation.
- No new laptop-sized audio model was found in the checked OpenRouter catalog:
  its suitable small entries are the already archived Nemotron Nano Omni,
  Voxtral Small, and Gemma models. Catalog absence does not imply no possible
  private deployment.

## Run requirements

Preserve the original seven questions. Verify attached audio bytes before scoring,
then distinguish sound identification, advice quality, and truncation. The
thunder prompt supplies both the sound and timing, so that item barely tests
listening; the other six clips carry most of the audio discrimination. Report the
small sample size and judge sensitivity rather than treating small score gaps as
precise rankings. Any local implementation must preserve the audio encoder,
preprocessing, chat template, and final-answer extraction.

NatureLM-audio has a dedicated local runner at
[`scripts/run_naturelm_audio.py`](scripts/run_naturelm_audio.py). It calls the
official sliding-window Python API, passes the unchanged benchmark system prompt
and question, hashes the exact MP3 bytes, and writes standard `bench.py` answer
and receipt files. Use `--merging-alpha` to record the checkpoint/base merge; the
default `1.0` is the published NatureLM checkpoint. The upstream macOS CLI currently
omits that required argument, so the runner intentionally bypasses the CLI.

The model's official processor truncates each inference window to 10 seconds.
With the default 10-second window and 10-second hop, most benchmark clips are
therefore evaluated from their first 10 seconds; the exactly 20-second thunder
clip receives two windows. This is an upstream model constraint, not a benchmark
scoring rule, and should be noted alongside any NatureLM results.

## Context for the MOSS 8B warning

The ambient-audio warning is RumiLabs' small deployment evaluation, not a broad
community consensus or proof that 4B is universally more accurate. Its seven-clip,
three-repeat notes describe truncation, digit repetition, and invented JSON
metadata. The authors report similar failures in the upstream PyTorch reference
and attribute the issue to that model rather than the MLX conversion alone.
The card mixes subset and whole-test denominators; do not infer a general failure
rate from it. The 4B port also reports occasional thinking-loop truncation and
weak fine-grained music recognition.

A concrete upstream request asks for timestamped, overlapping captions of a dog
barking, a woman speaking, and a car engine starting. Such event descriptions can
support recording search, descriptive captions, and acoustic context for agents.
Those applications need information a speech transcript omits. This benchmark's
animal recordings make the distinction especially relevant.

Sources: [RumiLabs 8B limitations](https://huggingface.co/RumiLabs/MOSS-Audio-8B-Thinking-MLX-hybrid#limitations),
[4B limitations](https://huggingface.co/RumiLabs/MOSS-Audio-4B-Thinking-MLX-4bit#limitations),
[timestamped-event request](https://github.com/OpenMOSS/MOSS-Audio/issues/16),
[MOSS technical report](https://arxiv.org/pdf/2606.01802).
