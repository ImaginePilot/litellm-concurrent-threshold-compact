# Instrument pitfalls

Every item here produced a confidently wrong number before it was caught. They are recorded because
each one is cheap to repeat and expensive to notice.

## 1. `content or reasoning_content` silently poisons the trunk

The summariser read `message.get("content") or message.get("reasoning_content")`. On a large span the
model spends its whole budget reasoning, returns `finish_reason="length"` with **empty content**, and
the fallback writes the model's own deliberation into the trunk — permanently, where it is served to
the agent as if it were a brief.

```
slice 165,811 chars
  max_tokens=700   finish=length  content=0     reasoning=2916   -> trunk gets REASONING
  max_tokens=2000  finish=stop    content=153   reasoning=4918   -> trunk gets CONTENT
```

Size-dependent, so it passes every small test. Three agentic runs completed before it was noticed.

**Fix:** never fall back to `reasoning_content` for stored state. Empty content is a *failure*, not a
note. Raise, don't substitute. Give the summariser enough budget to finish thinking **and** answer.

## 2. Your own reader is the second buffer

`subprocess.run(capture_output=True)` on a `curl -sN` stream blocks until the stream ends, so
per-token timestamps are assigned while parsing an already-finished buffer. This reported
**239,735 tok/s** and a TTFT equal to the whole request. `-sN` stops *curl* buffering; it does not
stop yours. Use `Popen` + `readline()`.

**Always run a single-lane control first, and check the median inter-token gap** (11.2 ms real vs
~4 µs broken) — not the span, which false-failed a working instrument because the model stopped early.

## 3. Check the exit code before reading any timing

Three separate speed figures in one session came from harnesses that had exited instantly on an
argument error — `21,772 fps`, `4,245 fps`, `3,780 fps`. Each project picks its own CLI convention
(`--frames N` vs positional `N`), and a rejected argument looks exactly like an extremely fast run.

## 4. The engine log is appended across restarts

`grep -q "ready:"` matched instantly against 79 lines from previous boots, so a restart-wait returned
immediately and benchmarked a still-loading engine. **Wait for the count to rise**, not for presence.
The same class of error: `grep -c` over a whole config file matched its own notes, not its args.

## 5. Gate the proxies for *every* A/B, including the ones that feel different

Two engine arms were run without stopping the routers, because they were config changes rather than
proxy changes. Production traffic ran throughout; the results contradicted both the source's own
measurements and prior runs, and had to be discarded. Ungated rounds on this box drift 1.24×.

## 6. Killing the local ssh does not kill the remote script

A cancelled benchmark left the fleet gated, with one router in `failed`, because the remote process
and its `trap … EXIT` survived. Kill the remote by PID, and arm a detached `setsid` watchdog that
restores services at a deadline regardless of what happens to the controlling session.

## 7. Warmup is not optional, and one round may not be enough

Benchmarking an engine 10 s after it reports `ready:` measures loading, not serving. Even with a
discarded warmup round, one arm's reps kept rising (171 → 176 → 187) while the other converged to
±0.5% — so the same statistic must be quoted for both, and a rising series should be reported as
unconverged rather than medianed.

## 8. Mixed traffic cannot answer a causal question

A global metrics ring carries every client. Correlating a variable against it mostly measures *which
client was running*. Attribution by joining to a per-request log with client identity, plus
stratifying **within** each client, is the minimum before claiming a relationship.
