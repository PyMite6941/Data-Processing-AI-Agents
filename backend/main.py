import sys
import io
import queue
import threading
import tempfile
import os
import re
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from bots import Bots

app = FastAPI()

# Accept localhost dev + any deployed frontend URL set via FRONTEND_URL env var
_origins = ["http://localhost:5173"]
if os.getenv("FRONTEND_URL"):
    _origins.append(os.getenv("FRONTEND_URL"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


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
    file_content = None
    file_suffix = ".csv"
    if file and file.filename:
        file_content = await file.read()
        file_suffix = os.path.splitext(file.filename)[1] or ".csv"

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
                bots.create_agents()
                bots.create_tasks()
                result = bots.create_crew(data_path or "")
                if result:
                    q.put({"__result__": result})
            except Exception as exc:
                q.put(f"[ERROR] {exc}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                if data_path and os.path.exists(data_path):
                    os.unlink(data_path)
                q.put(None)

        thread = threading.Thread(target=run_crew, daemon=True)
        thread.start()

        loop = asyncio.get_running_loop()
        while True:
            try:
                # Block up to 30s; if nothing arrives send a heartbeat to
                # keep Cloudflare's proxy (used by HF Spaces) from closing
                # the connection on the 100-second idle timeout.
                item = await loop.run_in_executor(
                    None, lambda: q.get(timeout=30)
                )
            except queue.Empty:
                yield ": ping\n\n"
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
