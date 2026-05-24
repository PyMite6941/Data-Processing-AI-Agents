import sys
import io
import queue
import threading
import tempfile
import os
import re
import time
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from bots import Bots

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

MAX_FILE_SIZE   = 10 * 1024 * 1024   # 10 MB
MAX_CONTEXT_LEN = 2000               # chars
MAX_RUNTIME     = 600                # seconds (10 min total pipeline timeout)
ALLOWED_EXTS    = {".csv", ".json", ".txt", ".pdf", ".xml"}


class LineCapture(io.TextIOBase):
    """Captures stdout/stderr line-by-line into a queue, stripping ANSI codes."""

    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text: str) -> int:
        cleaned = ANSI_ESCAPE.sub("", text)
        self._buf += cleaned
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.strip()
            if stripped:
                self._q.put(stripped)
        return len(text)

    def flush(self):
        if self._buf.strip():
            self._q.put(self._buf.strip())
            self._buf = ""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(context: str = Form(...), file: UploadFile = File(None)):
    # ── Input validation (before opening the SSE stream) ──────────────────────
    context = context.strip()
    if not context:
        return JSONResponse({"error": "Context is required."}, status_code=400)
    if len(context) > MAX_CONTEXT_LEN:
        return JSONResponse(
            {"error": f"Context too long ({len(context)} chars, max {MAX_CONTEXT_LEN})."},
            status_code=400,
        )

    file_content = None
    file_suffix = ".csv"
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            return JSONResponse(
                {"error": f"File type '{ext}' not supported. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"},
                status_code=400,
            )
        file_content = await file.read()
        if len(file_content) > MAX_FILE_SIZE:
            return JSONResponse(
                {"error": f"File too large ({len(file_content) // 1024}KB, max {MAX_FILE_SIZE // 1024 // 1024}MB)."},
                status_code=400,
            )
        file_suffix = ext

    # ── SSE stream ────────────────────────────────────────────────────────────
    async def event_stream():
        q: queue.Queue = queue.Queue()

        def run_crew():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            capture = LineCapture(q)
            sys.stdout = capture
            sys.stderr = capture
            data_path = None
            try:
                if file_content:
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=file_suffix
                    ) as tmp:
                        tmp.write(file_content)
                        data_path = tmp.name

                bots = Bots(context)
                result = bots.create_crew(data_path or "")
                if result:
                    q.put({"__result__": result})
            except Exception as exc:
                # Flush any buffered partial output before the error
                capture.flush()
                import traceback
                q.put(f"[ERROR] {exc}")
                for line in traceback.format_exc().splitlines():
                    if line.strip():
                        q.put(f"[TRACE] {line}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                if data_path and os.path.exists(data_path):
                    os.unlink(data_path)
                q.put(None)

        thread = threading.Thread(target=run_crew, daemon=True)
        thread.start()

        # CrewAI internal noise that clutters the stream during rotation/retry.
        # We surface our own [ROTATE] / [RETRY] lines instead.
        _NOISE = (
            "ERROR:root:",
            "ERROR:crewai.",
            "[CrewAIEventsBus]",
            "An unknown error occurred. Please check",
            "Error details: Error code:",
            "Error details: Model ",
        )

        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + MAX_RUNTIME

        while True:
            # Overall pipeline timeout guard
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield f"data: {json.dumps('[ERROR] Analysis timed out after 10 minutes.')}\n\n"
                yield f"data: {json.dumps('__DONE__')}\n\n"
                break

            try:
                item = await loop.run_in_executor(
                    None, lambda: q.get(timeout=min(30, remaining))
                )
            except queue.Empty:
                # Heartbeat keeps Cloudflare proxy from closing idle connection
                yield ": ping\n\n"
                continue

            # Drop internal CrewAI error noise during rotation; keep our own messages
            if isinstance(item, str) and any(item.startswith(p) or p in item for p in _NOISE):
                continue

            if item is None:
                yield f"data: {json.dumps('__DONE__')}\n\n"
                break
            if isinstance(item, dict) and "__result__" in item:
                yield f"data: {json.dumps({'type': 'result', 'content': item['__result__']})}\n\n"
            else:
                yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
