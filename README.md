# litellm-concurrent-threshold-compact

A transparent OpenAI-compatible proxy that applies **threshold context compaction** to agentic
traffic, with the compaction itself moved **off the request's critical path** and run concurrently
with the next model call.

Clients see an ordinary `/v1/chat/completions` endpoint. They keep sending their full conversation
history; the proxy rewrites it before forwarding. No client change is required.

---

## Why

NVIDIA's **"You don't need long context"** (Belcak, Muralidharan, Jiang, Fu, Byeon, Ye, Angilly,
Taghibakhshi, Liu, Ouyang, Heinrich, Molchanov — [arXiv:2611.05306](https://arxiv.org/abs/2611.05306),
2026‑11‑03) reports that frontier models "sacrifice little to no task accuracy when maintaining short
contexts" under frequent compaction, and that "later tokens are costlier to generate than earlier
tokens. On frontier open weight models, the disparity in cost may rise to as much as 7x."

The paper compares three regimes: full-history accumulation, **compaction at a threshold**, and
single-action compaction. The threshold regime scored best (74.8 on SWE-bench Verified against 70.0
for full history). This proxy implements that regime.

**Important caveat the paper itself raises.** Smaller open-weight models fared far worse — Qwen 3
Coder Next fell from 53.0% to 3.6% at the tightest budget. Anyone deploying this against a quantised
open model should measure task outcomes before trusting it.

---

## The three techniques

### 1. Threshold compaction (the paper's regime 2)

When the rendered context exceeds `T`, the span from the last cut up to a `KEEP_TAIL` window is
replaced by a short brief. Everything after stays verbatim. Compaction fires roughly once per
`T - KEEP_TAIL` tokens of growth rather than every turn, so each lossy re-encode covers ~10 turns
instead of 1 — which matters most for weaker models, where per-turn compaction compounds error.

### 2. Append-only trunk (bounded cost per event)

The naive implementation re-summarises the **whole history** at every compaction, so cost grows with
the conversation. Here the trunk is an ordered list of briefs `[c1, c2, … cN]`, and each event
summarises **only the delta span since the last cut**:

```
Trunk    = [c1, c2, c3 …]          one brief per compacted span
Rendered = [system] + [task verbatim] + [trunk] + [raw tail]
```

Nothing in the middle is ever rewritten, so the rendered prefix stays byte-stable between events and
the inference engine's prefix cache keeps hitting. Measured: per-event input stayed flat at
21–26k tokens across a session, instead of climbing to 140k.

### 3. Parallel (lagged) compaction — off the critical path

A request **never waits for a summary**. It is rendered from the trunk as it currently stands; if
that is still over `T`, a background task compacts the next span, landing in the trunk for the
*following* request. Span *N−1* is compacted while the model generates action *N* — lagged by
exactly one action.

```
request N    ──render from trunk──> upstream ──> client     (never blocks)
                   │
                   └─ background: summarise span N−1 ──> trunk (ready for N+1)
```

Measured effect: **504 s → 1.22 s** of blocking on the request path across a comparable session.

**Background concurrency must be bounded.** Unbounded, N conversations crossing `T` together launch
N concurrent summarisations; measured at 4-way they took 42–73 s each against 24–36 s solo, and they
competed with the foreground agents for the same GPUs. `COMPACTOR_BG_MAX` (default 1) caps it.

---

## Compaction prompt: a brief, not a summary

The paper's Appendix A is explicit that **"treating compaction as a mere special case of
summarization is suboptimal"** — LM summarisers "give equal weight to all facts" and "overemphasize
repeated segments … regardless of their ultimate relevance to the tail of the trace". The best
compactions "are not narratives of the execution history but carefully cherry-picked briefs for
further execution".

So the prompt here is forward-looking and records **pointers, not contents** — the agent can re-read
any file. Six sections, following the paper's published prompt: Objective / Established facts (exact
paths and symbols; observations separated from unverified ideas) / Applied changes (and whether
verified) / Verification / **Dead ends** / Remaining plan.

The task is held out of every compacted span and replayed **verbatim**, matching the paper's reset to
"the system prompt, task, and this recap".

What re-reading *cannot* recover is what the brief must carry: dead ends (a rejected approach leaves
no trace on disk), verified-vs-merely-applied, the task's completion conditions, and the next action.

---

## Architecture

```
client ──> :6001  (this proxy: compaction only)
              └──> :8000  (LiteLLM: reasoning replay, usage logging)
                      └──> :8080  (inference engine)
```

Chaining in front of an existing router rather than replacing it keeps the A/B exact: both arms
traverse the same stack and differ only by this layer.

A relay on `:6000` forwards into the same instance, because **Node's `undici` refuses port 6000**
(WHATWG fetch blocked-port list, X11) with `Error: bad port` — curl works, Node clients do not. One
instance behind both ports; two instances would split the sticky-compaction state.

## Configuration

| env | default | meaning |
|---|---|---|
| `COMPACTOR_UPSTREAM` | `http://127.0.0.1:8000/v1` | where to forward |
| `COMPACTOR_T` | `40000` | compaction trigger (estimated tokens) |
| `COMPACTOR_KEEP_TAIL` | `12000` | verbatim tail retained |
| `COMPACTOR_BG_MAX` | `1` | max concurrent background summarisations |
| `COMPACTOR_TRUNK_MAX` | `8000` | trunk size before folding older briefs |
| `COMPACTOR_SUM_MAXTOK` | `2500` | budget per brief |
| `COMPACTOR_PASSTHROUGH_SUFFIX` | `-raw` | model-name suffix that bypasses compaction |

`/stats` exposes counters; every compaction is logged with its brief text to `COMPACTOR_LOG`.

---

See `data/MEASUREMENTS.md` for the measured results and `data/PITFALLS.md` for the instrument bugs
found while measuring — several of which produced confidently wrong numbers before being caught.
