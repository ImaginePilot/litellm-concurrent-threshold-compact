# Measurements

Hardware: 4× RTX 3080‑20G (pipeline parallel, 4 stages, layer split 13,26,39), 125B MoE at IQ3_XXS,
`--kv int8 --kv-resident 65536`, conversation cache 12288 MiB. Host: 2× Xeon Platinum 8269CY,
376 GiB.

**Every A/B below stopped the three LiteLLM routers first.** Ungated warm rounds on this box drift
1.24×, which is larger than most effects worth measuring. The one arm where this rule was skipped is
marked and excluded.

---

## 1. Initial (long context) vs latest (threshold compaction) — the headline comparison

### 1a. Controlled decode-vs-context sweep (gated, 4 lanes, 3 reps/arm, prefill excluded)

The measured request appends a short turn so it hits the cached prefix; per-token timestamps are
taken on arrival. Within-arm spread on per-lane decode ≤1%.

| context | per-lane decode | fleet decode | TTFT |
|---|---|---|---|
| 20k | 74.2 tok/s | 269.2 tok/s | 1.75 s |
| **40k** (compacted target) | **68.4** | **237.0** | **2.35 s** |
| 80k | 59.0 | 187.7 | 3.23 s |
| **106k** (uncompacted) | **54.5** | **166.6** | **8.79 s** |

**106k → 40k buys +42% fleet decode, +26% per-lane, and cuts TTFT by 73%.**

Note the slope is gentle: −27% decode across a 5.3× context range. That is far flatter than the
paper's 3.6–7×, which is quoted for 16k vs **256k** — three times further out than this workload
reaches. Do not expect the paper's cost multiplier at these context lengths.

### 1b. The cliff is the cache budget, not the context

Re-running 80k with a larger resident set (same context, same config, only the conversation-cache
pressure differs):

| 80k | within budget | over budget | delta |
|---|---|---|---|
| per-lane | 59.0 | 46.1 | −22% |
| fleet decode | 187.7 | 98.8 | **−47%** |
| TTFT | 3.23 s | 9.94 s | 3.1× |

**That −47% is larger than the entire 20k→106k context effect (−38%).** Working set from the fitted
park model (`291 MiB + 19.4 KB/token`, ×4 lanes) against the 12 GiB budget:

| context | per conversation | 4 agents | vs 12 GiB |
|---|---|---|---|
| 40k | 1.02 GiB | **4.1 GiB** | 8 GiB headroom |
| 80k | 1.80 | 7.2 | marginal |
| 106k | 2.31 | 9.2 | + cold arrivals → thrash |

So the main value of compaction here is **structural**: it makes the working set fit, rather than
making tokens cheaper. At the uncompacted size the budget is already marginal before cold
conversations arrive.

### 1c. End-to-end agentic run (one sample — see caveats)

Same task (build a slither.io game in pygame, multi-module, headless smoke test, README) driven by a
coding agent:

| | requests | wall | s/request | arm tok/s | median per-req |
|---|---|---|---|---|---|
| latest (compaction, fixed summariser) | 186 | 1930 s | **10.4** | **65.8** | **76.3** |

Context held at median 47.0k / p90 57.9k against an uncompacted run that reached 140k estimated.

---

## 2. Parallel (lagged) compaction vs serial

| | serial | **parallel** |
|---|---|---|
| blocking on request path | **504 s** | **1.22 s** |
| per-event input | grew to 140k | flat 21–26k |
| cut position | **froze** (boundary bug) | advanced 1→45→109→161→223→281→349 |
| summarisation | inline | 608 s, all background |

## 3. Background concurrency must be bounded

4 conversations crossing `T` within 0.2 s launched 4 concurrent summarisations:

| | solo | 4-way |
|---|---|---|
| duration each | 24.5 / 30.2 / 36.3 / 31.2 s | 42.7 / 41.6 / **72.7** / 42.5 s |

With `BG_MAX=1`, peak concurrency returned to 1 and durations to 15.9–31.4 s.

**Second-order effect, engine-specific:** on this engine the MTP speculative drafter stops firing
entirely at ≥3 concurrent requests. Bounding background compaction raised draft firing from 53% to
68% of requests and median decode by +22%. Observational, not a controlled manipulation.

| concurrency | n | drafts/token | tok/s |
|---|---|---|---|
| 1–2 | 11 | 0.861 | 129.5 |
| 2–3 | 21 | 0.764 | 125.3 |
| **3–5** | 14 | **0.000** | 85.4 |

Context showed **no** monotonic relationship with draft firing; concurrency did. Verified within each
client independently, so it is not a client artifact.

## 4. Slot count at the compacted context (gated, 3 reps + discarded warmup, 50.2k/lane)

| | batch 4 | batch 8 |
|---|---|---|
| reps | 171.2 / 176.1 / 187.4 (**still rising**) | 210.2 / 212.3 / 211.9 (**±0.5%**) |
| per-lane | 42.8–46.9 | 26.5 |
| latency p100 | 8.5–9.3 s | 15.1 s |
| expert cache | 6656 / 9.41 GiB | **unchanged** |

+13% aggregate quoted conservatively against batch 4's best rep, since batch 4 never converged.
~1.5 GiB VRAM per slot at this context. Adopted mainly for a structural reason: 4 agents plus one
background summariser peak at 5 concurrent streams against 4 slots, so a background summary evicts an
agent's conversation.

---

## What is NOT established

- **No task-quality comparison.** Three of four agentic arms ran with a broken summariser (see
  PITFALLS §1), so only the final arm is valid — one sample, no control.
- **Compaction fidelity is unmeasured.** The only gate is that the brief is non-empty.
- **The n=1 arms cannot attribute causation.** Agent trajectories vary substantially run to run.
- **MTP findings are observational**, from overlapping request spans rather than true slot occupancy.
