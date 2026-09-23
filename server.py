"""Sarkari Sahayak web server (FastAPI). Run:  python server.py   ->  http://127.0.0.1:8000"""
import os
import time
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import engine

app = FastAPI(title="Sarkari Sahayak")
TOOLS = engine.TOOLS


def _lang(x):
    return x if x in engine.LANGS else "en"


def _timed(trace, name, detail, fn, *a, **kw):
    """Call a tool and record it in the agent trace shown in the UI."""
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    trace.append({"tool": name, "detail": detail(out) if callable(detail) else detail,
                  "ms": round((time.perf_counter() - t0) * 1000)})
    return out


@app.get("/api/health")
def health():
    return {"ok": True, "llm": bool(os.environ.get("GEMINI_API_KEY")), "verified_on": engine.DATA["meta"]["verified_on"]}


@app.post("/api/chat")
def chat(body: dict = Body(...)):
    """text (optional) + profile so far + answers from quick-reply chips -> eligibility results."""
    lang, trace = _lang(body.get("lang")), []
    profile = dict(body.get("profile") or {})
    text = (body.get("text") or "").strip()
    if text:
        parsed = _timed(trace, "parse_citizen_text",
                        lambda o: f"{o['method']} -> {', '.join(o['profile']) or 'nothing found'}", TOOLS["parse_citizen_text"], text, lang)
        profile.update(parsed["profile"])
    profile.update(body.get("answers") or {})
    for k in [k for k, v in profile.items() if v is None]:
        profile.pop(k)  # the UI sends null to "forget" a fact
    res = _timed(trace, "check_eligibility",
                 lambda o: o["summary"].split(": ", 1)[-1][:90], TOOLS["check_eligibility"], profile, lang)
    res["trace"] = trace
    res["raw_profile"] = profile  # only what the user/extractor stated (no derived values)
    return res


@app.post("/api/pdf")
def pdf(body: dict = Body(...)):
    lang, sid = _lang(body.get("lang")), body.get("scheme_id")
    if sid not in engine.SCHEMES:
        return Response("unknown scheme", status_code=404)
    data = engine.build_pdf(sid, body.get("profile") or {}, lang)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="sarkari-sahayak-{sid}-{lang}.pdf"'})


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

if __name__ == "__main__":
    print("\n  Sarkari Sahayak running ->  http://127.0.0.1:8000\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
