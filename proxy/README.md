# Running it

```bash
pip install fastapi uvicorn httpx

COMPACTOR_UPSTREAM=http://127.0.0.1:8000/v1 \
COMPACTOR_T=40000 \
COMPACTOR_BG_MAX=1 \
uvicorn compactor:app --host 0.0.0.0 --port 6001 --workers 1

# optional: also answer on :6000 (same instance, shared sticky state)
python3 relay6000.py
```

`--workers 1` matters: the sticky-cut state is per-process, and multiple workers would split it.

Point an OpenAI-compatible client at `http://<host>:6001/v1`. Use a model name ending in `-raw` to
bypass compaction for a control arm on the identical stack.
