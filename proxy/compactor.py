#!/usr/bin/env python3
"""
compactor v2 - PARALLEL (lagged) threshold compaction.

  client -> :6001 (THIS) -> :8000 (reasoning replay + usage) -> :8080 engine

v1 was serial: it awaited the summariser inline, so every compaction stalled the
request (504 s of critical path over one session) and it re-summarised the WHOLE
head each time, so cost grew with the conversation.

v2 implements the user's parallel strategy:

  * APPEND-ONLY TRUNK.  Trunk = [c1, c2, ... cN], one note per compacted span.
    A compaction summarises ONLY msgs[cut:new_cut] - the delta since the last
    cut - so each event's cost is bounded by one span, not by the whole history.
    Nothing in the middle is ever rewritten, so the rebuilt prefix stays stable
    and the engine's prefix cache keeps hitting.

  * OFF THE CRITICAL PATH.  A request NEVER waits for a summary. It is rebuilt
    from the trunk as it stands, and if that is still over T a background task is
    launched to compact the next span. That summary lands in the trunk for the
    FOLLOWING request - i.e. span N-1 is compacted while the model generates
    action N. Compaction is therefore lagged by exactly one action, which is the
    intended trade: the context carries one extra raw span in exchange for zero
    stall.

  * FIXED BOUNDARY RULE (v1 bug).  v1 would only cut at a `user` message. An
    agentic loop is almost entirely assistant/tool pairs, so no boundary appeared
    and the cut froze at message 96 while the tail grew to 79k est tokens. v2
    cuts at any index that neither IS a tool result nor immediately FOLLOWS an
    assistant carrying tool_calls - never orphaning a tool result, but with many
    more legal boundaries.
"""
import asyncio, hashlib, json, os, time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM   = os.environ.get("COMPACTOR_UPSTREAM", "http://127.0.0.1:8000/v1")
T_TRIGGER  = int(os.environ.get("COMPACTOR_T", "40000"))
KEEP_TAIL  = int(os.environ.get("COMPACTOR_KEEP_TAIL", "12000"))
TRUNK_MAX  = int(os.environ.get("COMPACTOR_TRUNK_MAX", "8000"))
# Global cap on CONCURRENT background summarisations across ALL conversations.
# Measured 2026-11-14: 4 agents crossing T together launched 4 at once and each took
# 42-73 s versus 24-36 s solo - they contend with each other AND with the decode lanes,
# which are already at 98% of the 1280 W pool cap at 4 lanes. 1 keeps compaction in the
# gaps instead of bidding for a 5th-8th lane.
BG_MAX     = int(os.environ.get("COMPACTOR_BG_MAX", "1"))
# 2500, not 700: on a 20k-token span the model spends the whole budget REASONING and
# finishes with finish_reason="length" and an EMPTY content field. Measured 2026-11-14.
SUM_MAXTOK = int(os.environ.get("COMPACTOR_SUM_MAXTOK", "2500"))
SUM_MODEL  = os.environ.get("COMPACTOR_SUM_MODEL", "qwen3.8-flash-next")
LOG        = os.environ.get("COMPACTOR_LOG", "/tmp/compactor2.jsonl")
PASSTHRU   = os.environ.get("COMPACTOR_PASSTHROUGH_SUFFIX", "-raw")
CHARS_PER_TOK = 3.4

app = FastAPI()
STATE: dict = {}
LOCKS: dict = {}
BG_SEM = None
STATS = {"requests":0,"spans_compacted":0,"folds":0,"bg_launched":0,"bg_done":0,
         "bg_wait_s":0.0,"bg_peak_concurrent":0,"bg_inflight":0,
         "bg_failed":0,"blocking_s":0.0,"background_s":0.0,"passthrough":0,
         "rebuilt":0,"under":0,"no_boundary":0,"errors":0}

def est(o):
    try: return int(len(json.dumps(o, ensure_ascii=False)) / CHARS_PER_TOK)
    except Exception: return 0

def jlog(r):
    r["t"] = time.time()
    try:
        with open(LOG,"a") as f: f.write(json.dumps(r, ensure_ascii=False)+"\n")
    except Exception: pass

def fp_of(msgs):
    base = msgs[:2] if len(msgs) >= 2 else msgs
    return hashlib.sha256(json.dumps(base,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]

def safe_cut(msgs, start, keep_tokens):
    """Largest i>start where msgs[i:] >= keep_tokens and cutting at i orphans nothing."""
    acc = 0; i = len(msgs)
    while i > start + 1:
        i -= 1
        acc += est(msgs[i])
        if acc < keep_tokens: continue
        m = msgs[i]
        if m.get("role") == "tool" or m.get("tool_call_id"): continue
        prev = msgs[i-1] if i >= 1 else {}
        if prev.get("role") == "assistant" and prev.get("tool_calls"): continue
        return i
    return None

SYS_P = (
 "Stop work. Write only a concise handoff recap; do not call tools. The conversation will be "
 "reset to the system prompt, the task, and this recap, so the recap must carry everything "
 "needed to continue.\n"
 "This is NOT a summary. Do not narrate what happened. Select only what the REMAINING work "
 "needs. The agent can re-read any file, so record POINTERS (exact paths, symbols, commands) "
 "rather than file contents.\n"
 "Record:\n"
 "1. Objective - one line describing the required result.\n"
 "2. Established facts - exact paths, relevant symbols or commands, observed values, and "
 "important errors/output. Separate observations from unverified ideas.\n"
 "3. Applied changes - each changed/created file, what changed, and whether verified.\n"
 "4. Verification - exact checks run, outcomes, failures, obstacles, and workarounds.\n"
 "5. Dead ends - failed approaches that should not be repeated.\n"
 "6. Remaining plan - outstanding subgoals in execution order; the immediate next command or "
 "edit; and the condition for completion.\n"
 "If needed output was truncated, give a narrow command to retrieve it. "
 "This recap is appended after earlier recaps, so do not restate what they already cover.")

async def summarise(span, client, prompt=SYS_P):
    body = {"model":SUM_MODEL,
            "messages":[{"role":"system","content":prompt},
                        {"role":"user","content":"Compress this slice:\n\n"
                         + json.dumps(span, ensure_ascii=False)[:400000]}],
            "max_tokens":SUM_MAXTOK,"temperature":0.0,"stream":False}
    r = await client.post(f"{UPSTREAM}/chat/completions", json=body, timeout=600.0)
    r.raise_for_status()
    j = r.json(); ch = j["choices"][0]; m = ch["message"]
    content = (m.get("content") or "").strip()
    # NEVER fall back to reasoning_content. When the budget runs out mid-thought the model
    # returns finish_reason="length" with empty content, and the fallback would write the
    # summariser's own deliberation ("We need answer user's request...") into the trunk,
    # where it is permanent and unrecoverable. An empty note is a failure, not a note.
    if not content:
        raise RuntimeError("summariser produced no content (finish=%s, reasoning_chars=%d)"
                           % (ch.get("finish_reason"), len(m.get("reasoning_content") or "")))
    return content

def rebuild(msgs, trunk, cut):
    """[system] + [task verbatim] + [ordered recaps] + [raw tail].

    The task is held out of every compacted span: the paper's regimes reset to
    "the system prompt, task, and this recap", and a task that has been through a
    summariser is the one loss the agent cannot recover by re-reading the repo.
    """
    if not trunk or cut <= 2: return msgs
    note = "\n\n".join("[recap %d] %s" % (i+1, c) for i, c in enumerate(trunk))
    head = [msgs[0]]
    if len(msgs) > 1 and msgs[1].get("role") == "user":
        head.append(msgs[1])                      # verbatim task
    return (head +
            [{"role":"user","content":"[HANDOFF RECAPS - earlier turns of this session. "
              "Files on disk are authoritative; re-read them rather than trusting detail "
              "recalled here.]\n" + note},
             {"role":"assistant","content":"Acknowledged. Continuing from that state."}]
            + msgs[cut:])

async def bg_compact(fp, snapshot, old_cut, new_cut):
    """Runs CONCURRENTLY with the model generating the next action, but at most
    BG_MAX of these run at once across all conversations."""
    global BG_SEM
    if BG_SEM is None:
        BG_SEM = asyncio.Semaphore(BG_MAX)   # single-threaded loop: no await, no race
    tq = time.time()
    async with BG_SEM:
        STATS["bg_wait_s"] += time.time() - tq
        STATS["bg_inflight"] += 1
        STATS["bg_peak_concurrent"] = max(STATS["bg_peak_concurrent"], STATS["bg_inflight"])
        try:
            await _bg_compact_inner(fp, snapshot, old_cut, new_cut)
        finally:
            STATS["bg_inflight"] -= 1

async def _bg_compact_inner(fp, snapshot, old_cut, new_cut):
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0)) as c:
            span = snapshot[max(2, old_cut):new_cut]   # task (index 1) held out
            note = await summarise(span, c)
            if not note:
                STATS["bg_failed"] += 1; return
            lock = LOCKS.setdefault(fp, asyncio.Lock())
            async with lock:
                st = STATE.setdefault(fp, {"trunk":[], "cut":1, "pending":None})
                if new_cut > st["cut"]:
                    st["trunk"].append(note); st["cut"] = new_cut
                    STATS["spans_compacted"] += 1
                # fold the trunk if it has itself grown past budget
                if est(st["trunk"]) > TRUNK_MAX and len(st["trunk"]) > 2:
                    head = st["trunk"][:-1]
                    folded = await summarise(
                        [{"role":"user","content":"\n\n".join(head)}], c,
                        "Merge these ordered state notes into ONE shorter note, losing no "
                        "decision, file, signature, or outstanding item. Terse bullets.")
                    if folded:
                        st["trunk"] = [folded, st["trunk"][-1]]; STATS["folds"] += 1
            STATS["bg_done"] += 1
            dt = time.time() - t0; STATS["background_s"] += dt
            jlog({"ev":"bg_compact","fp":fp,"old_cut":old_cut,"new_cut":new_cut,
                  "span_est":est(span),"note_tok":est(note),"s":round(dt,2),
                  "trunk_chunks":len(STATE[fp]["trunk"]),"trunk_est":est(STATE[fp]["trunk"]),
                  # persist the note text: without it a bad summary is unauditable after
                  # the process restarts, and STATE is the only other copy.
                  "note":note})
    except Exception as e:
        STATS["bg_failed"] += 1
        jlog({"ev":"bg_failed","fp":fp,"err":str(e)[:200]})
    finally:
        st = STATE.get(fp)
        if st and st.get("pending") and st["pending"].done(): st["pending"] = None
        elif st: st["pending"] = None

async def prepare(payload):
    """NEVER awaits a summary. Rebuilds from the trunk as it stands and may launch
    a background compaction for the next request to benefit from."""
    msgs = payload.get("messages") or []
    if len(msgs) < 4:
        STATS["under"] += 1; return payload, {"action":"short"}
    total = est(msgs)
    fp = fp_of(msgs)
    lock = LOCKS.setdefault(fp, asyncio.Lock())
    async with lock:
        st = STATE.setdefault(fp, {"trunk":[], "cut":1, "pending":None})
        trunk, cut = list(st["trunk"]), st["cut"]
        if cut > len(msgs): trunk, cut = [], 1; st["trunk"], st["cut"] = [], 1
        out = rebuild(msgs, trunk, cut)
        after = est(out)
        launched = False
        if after > T_TRIGGER and (st["pending"] is None or st["pending"].done()):
            nc = safe_cut(msgs, max(cut, 2), KEEP_TAIL)
            if nc and nc > cut:
                st["pending"] = asyncio.create_task(bg_compact(fp, list(msgs), cut, nc))
                STATS["bg_launched"] += 1; launched = True
            else:
                STATS["no_boundary"] += 1
    if trunk: STATS["rebuilt"] += 1
    payload["messages"] = out
    return payload, {"action":"rebuilt" if trunk else ("over" if after > T_TRIGGER else "under"),
                     "est_before":total,"est_after":after,"cut":cut,
                     "trunk_chunks":len(trunk),"trunk_est":est(trunk),"bg_launched":launched}

@app.get("/v1/models")
@app.get("/models")
async def models():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{UPSTREAM}/models", timeout=30.0)
        return JSONResponse(r.json(), status_code=r.status_code)

@app.get("/stats")
async def stats():
    s = dict(STATS)
    s.update({"live_conversations":len(STATE),"T":T_TRIGGER,"keep_tail":KEEP_TAIL,"bg_max":BG_MAX,
              "trunk_max":TRUNK_MAX,"upstream":UPSTREAM,"version":2,
              "trunks":{k:{"chunks":len(v["trunk"]),"cut":v["cut"],
                           "est":est(v["trunk"])} for k,v in list(STATE.items())[:6]}})
    return JSONResponse(s)

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat(request: Request):
    payload = await request.json(); STATS["requests"] += 1
    model = payload.get("model","")
    client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=30.0))
    t0 = time.time()
    try:
        if model.endswith(PASSTHRU):
            payload["model"] = model[:-len(PASSTHRU)]; STATS["passthrough"] += 1
            info = {"action":"passthrough"}
        else:
            payload, info = await prepare(payload)
        STATS["blocking_s"] += time.time() - t0
        url = f"{UPSTREAM}/chat/completions"
        if not payload.get("stream"):
            r = await client.post(url, json=payload); out = r.json()
            jlog({"ev":"req","stream":False,**info}); await client.aclose()
            return JSONResponse(out, status_code=r.status_code)
        async def gen():
            try:
                async with client.stream("POST", url, json=payload) as r:
                    async for chunk in r.aiter_raw(): yield chunk
            finally:
                await client.aclose()
        jlog({"ev":"req","stream":True,**info})
        return StreamingResponse(gen(), media_type="text/event-stream")
    except Exception as e:
        STATS["errors"] += 1; jlog({"ev":"error","err":str(e)[:300]})
        try: await client.aclose()
        except Exception: pass
        return JSONResponse({"error":{"message":str(e)[:300],"type":"compactor_error"}}, status_code=500)
