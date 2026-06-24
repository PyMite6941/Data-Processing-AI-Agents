"""
DataFlow AI — single-file backend.
FastAPI SSE server + CrewAI 6-agent pipeline.
"""

# ── Stdlib ────────────────────────────────────────────────────────────────────
import sys
import io
import queue
import threading
import tempfile
import os
import re
import re as _re
import time
import time as _time
import asyncio
import json
import json as _json
from threading import Lock
from typing import Optional, Literal, List as _List

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from crewai import Agent, Crew, Task, Process, LLM
from crewai_tools import FileReadTool

import litellm
litellm.cache = None
litellm.drop_params = True

# ── Groq cache_breakpoint patch ───────────────────────────────────────────────
_real_completion = litellm.completion

def _completion_no_cache_breakpoint(*args, **kwargs):
    kwargs["caching"] = False
    for msg in kwargs.get("messages", []):
        if isinstance(msg, dict):
            msg.pop("cache_breakpoint", None)
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_breakpoint", None)
    return _real_completion(*args, **kwargs)

litellm.completion = _completion_no_cache_breakpoint

# ── Model rotation pools ──────────────────────────────────────────────────────
_FAST_MODELS = [
    "github/Phi-3.5-mini-instruct",
    "github/AI-Model-Gemma-2-2B-it",
    "github/Llama-3.1-8B-Instruct",
    "openrouter/nvidia/nemotron-nano-9b-v2:free",
    "openrouter/minimax/minimax-m2.5:free",
    "openrouter/meta-llama/llama-3.1-8b-instruct:free",
    "openrouter/mistralai/mistral-7b-instruct:free",
    "openrouter/google/gemma-3-12b-it:free",
    "openrouter/qwen/qwen3-8b:free",
    "openrouter/meta-llama/llama-4-scout:free",
    "openrouter/microsoft/phi-3-mini-128k-instruct:free",
]
_SMART_MODELS = [
    "github/Llama-3.1-70B-Instruct",
    "github/Mistral-large-2407",
    "github/gpt-4o-mini",
    "openrouter/google/gemma-3-27b-it:free",
    "openrouter/qwen/qwen3-coder:free",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/deepseek/deepseek-chat-v3-0324:free",
    "openrouter/meta-llama/llama-4-maverick:free",
    "openrouter/mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/google/gemma-3-12b-it:free",
]

# ── Cooldown state ────────────────────────────────────────────────────────────
_cooldown: dict[str, float] = {}
_cooldown_lock = Lock()
_crew_lock = Lock()


def _set_cooldown(model: str, seconds: float) -> None:
    until = _time.monotonic() + seconds
    with _cooldown_lock:
        _cooldown[model] = max(_cooldown.get(model, 0.0), until)


def _pick_model(pool: list[str]) -> tuple[str, int]:
    now = _time.monotonic()
    with _cooldown_lock:
        for idx, model in enumerate(pool):
            if _cooldown.get(model, 0.0) <= now:
                return model, idx
        best = min(range(len(pool)), key=lambda i: _cooldown.get(pool[i], 0.0))
        return pool[best], best


def _wait_until_available() -> None:
    now = _time.monotonic()
    with _cooldown_lock:
        fast_waits = [max(0.0, _cooldown.get(m, 0.0) - now) for m in _FAST_MODELS]
        smart_waits = [max(0.0, _cooldown.get(m, 0.0) - now) for m in _SMART_MODELS]
    sleep_s = max(min(fast_waits), min(smart_waits))
    if sleep_s > 0:
        print(f"[WAIT] All providers cooling — resuming in {sleep_s:.0f}s")
        _time.sleep(sleep_s)


def _parse_retry_after(err_str: str) -> float:
    m = _re.search(r"retry_after_seconds['\"\s:]+(\d+(?:\.\d+)?)", err_str)
    if m:
        return float(m.group(1)) + 5
    m = _re.search(r"[Pp]lease try again in (\d+(?:\.\d+)?)s", err_str)
    if m:
        return float(m.group(1)) + 2
    return 35.0


def _extract_json(text: str) -> str:
    text = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.MULTILINE)
    text = _re.sub(r"\s*```$", "", text.strip(), flags=_re.MULTILINE)
    text = text.strip()
    try:
        _json.loads(text)
        return text
    except _json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start: i + 1]
                    try:
                        _json.loads(candidate)
                        return candidate
                    except _json.JSONDecodeError:
                        break
    return text


_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
if _OR_KEY:
    os.environ.setdefault("OPENAI_API_KEY", _OR_KEY)

GITHUB_API_BASE = "https://models.inference.ai.azure.com"


def _api_key_for(model: str) -> str | None:
    if model.startswith("groq/"):
        return os.getenv("GROQ_API_KEY")
    if model.startswith("github/"):
        return os.getenv("GITHUB_TOKEN")
    return os.getenv("OPENROUTER_API_KEY")


def _resolve_model(model: str) -> tuple[str, str | None]:
    if model.startswith("github/"):
        name = model[len("github/"):]
        return f"openai/{name}", GITHUB_API_BASE
    return model, None


# ── Output schemas ────────────────────────────────────────────────────────────

class DataPoint(BaseModel):
    label: str
    value: float
    category: Optional[str] = None
    x_value: Optional[float] = None
    value2: Optional[float] = None


class CodeBlock(BaseModel):
    language: str
    title: str
    code: str


class MetricItem(BaseModel):
    label: str
    value: str
    unit: Optional[str] = None
    trend: Optional[str] = None
    change: Optional[str] = None
    context: Optional[str] = None


class ComparisonRow(BaseModel):
    metric: str
    value_a: str
    value_b: str
    winner: Optional[Literal["a", "b", "tie"]] = None


class FormattedOutput(BaseModel):
    output_type: Literal["chart", "report", "code", "table", "metrics", "comparison", "heatmap"]
    chart_type: Optional[Literal["bar", "line", "pie", "scatter", "funnel", "radar"]] = None
    chart_title: Optional[str] = None
    x_axis_label: Optional[str] = None
    y_axis_label: Optional[str] = None
    data_points: Optional[list[DataPoint]] = None
    radar_b_label: Optional[str] = None
    code_blocks: Optional[list[CodeBlock]] = None
    table_headers: Optional[list[str]] = None
    table_rows: Optional[list[list[str]]] = None
    metrics: Optional[list[MetricItem]] = None
    comparison_a_label: Optional[str] = None
    comparison_b_label: Optional[str] = None
    comparison_rows: Optional[list[ComparisonRow]] = None
    heatmap_title: Optional[str] = None
    heatmap_row_labels: Optional[list[str]] = None
    heatmap_col_labels: Optional[list[str]] = None
    heatmap_values: Optional[list[list[float]]] = None
    summary: str
    findings: list[str]
    recommendations: list[str]
    quality_score: Optional[int] = None
    quality_verdict: Optional[str] = None


# ── Agent pipeline ────────────────────────────────────────────────────────────

class Bots:
    def __init__(self, context: str):
        self.context = context
        self._ctx = context.replace("{", "{{").replace("}", "}}")
        self._fast_idx = 0
        self._smart_idx = 0
        self.file_read = FileReadTool()

    def _make_llm(self, pool: list[str], temperature: float, max_tokens: int = 1024) -> LLM:
        model = pool[self._fast_idx % len(pool)] if pool is _FAST_MODELS else pool[self._smart_idx % len(pool)]
        actual_model, api_base = _resolve_model(model)
        kwargs = dict(
            model=actual_model,
            api_key=_api_key_for(model),
            max_tokens=max_tokens,
            max_retries=0,
            timeout=120,
            temperature=temperature,
        )
        if api_base:
            kwargs["api_base"] = api_base
        return LLM(**kwargs)

    def _smart_llm(self, temperature: float) -> LLM:
        return self._make_llm(_SMART_MODELS, temperature)

    def _fast_llm(self, temperature: float, max_tokens: int = 1024) -> LLM:
        return self._make_llm(_FAST_MODELS, temperature, max_tokens)

    def create_agents(self):
        self.context_agent = Agent(
            role="Analysis Directive Specialist",
            goal=(
                "Read the user's raw context and rewrite it as a precise, unambiguous "
                "analysis directive. Identify the core question, the most relevant columns "
                "or metrics, and the exact type of analysis needed."
            ),
            backstory=(
                "You translate vague requests into sharp, actionable instructions. "
                "You never perform analysis — you only clarify the directive."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._fast_llm(0.2),
            allow_delegation=False,
            cache=False,
        )

        self.data_cleaner = Agent(
            role="Data Quality Inspector",
            goal=(
                "Read every file provided and produce a concise data quality report: "
                "column names, row count, missing values, duplicate rows, data type issues. "
                "Keep under 200 words."
            ),
            backstory=(
                "You are a meticulous data auditor. You use FileReadTool once per file, "
                "then summarise its structure and flag obvious problems."
            ),
            tools=[self.file_read],
            verbose=True,
            memory=False,
            max_iter=5,
            llm=self._fast_llm(0.1, max_tokens=512),
            allow_delegation=False,
            cache=False,
        )

        self.prompt_engineer = Agent(
            role="Data Analysis Prompt Engineer",
            goal=(
                "Construct a precise, step-by-step analysis prompt for the data analyst. "
                "Specify exact columns, calculations, patterns to look for, and order of steps."
            ),
            backstory=(
                "You write technical prompts for data analysis pipelines. "
                "Vague instructions produce vague results — you are never vague."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._fast_llm(0.4),
            allow_delegation=False,
            cache=False,
        )

        self.data_analyst = Agent(
            role="Senior Data Analyst",
            goal=(
                "Follow the analysis prompt exactly. Call FileReadTool ONCE per file path. "
                "Reason over the content to answer the prompt. Never speculate beyond the data."
            ),
            backstory=(
                "You are a rigorous analyst. You call FileReadTool exactly once per file — "
                "re-reading wastes tokens. You back every finding with evidence."
            ),
            tools=[self.file_read],
            verbose=True,
            memory=False,
            max_iter=6,
            llm=self._smart_llm(0.1),
            allow_delegation=False,
            cache=False,
        )

        self.output_formatter = Agent(
            role="Structured Output Specialist",
            goal=(
                "Convert analyst findings into a strict FormattedOutput JSON object. "
                "Choose output_type by priority: code → metrics → comparison → heatmap → table → chart → report."
            ),
            backstory=(
                "You serialise analysis results into one of 7 output modes:\n"
                "• code       — runnable scripts, queries, or algorithms\n"
                "• metrics    — key numbers / KPIs (3-8 items)\n"
                "• comparison — two named entities compared across metrics\n"
                "• heatmap    — matrix of values (correlation, frequency, activity)\n"
                "• table      — ranked/multi-attribute list (max 20 rows)\n"
                "• chart      — bar, line, pie, scatter, funnel, or radar\n"
                "• report     — qualitative or narrative findings\n\n"
                "CHART TYPE SELECTION:\n"
                "  funnel → sequential conversion stages with drop-off\n"
                "  radar  → multi-attribute profile (use value2+radar_b_label for dual series)\n"
                "  scatter → correlation (x_value + value per point)\n"
                "  pie → 2-6 parts of a whole\n"
                "  line → time series\n"
                "  bar → named categories\n\n"
                "COMPARISON: comparison_a_label and comparison_b_label name the two entities. "
                "Each comparison_row has metric, value_a, value_b, and winner (a/b/tie).\n\n"
                "HEATMAP: heatmap_values is a 2D list [row][col] of floats. Max 10×10.\n\n"
                "Output ONLY the raw JSON object. No markdown fences, no preamble. "
                "summary=2-3 sentences, findings=3-5 strings, recommendations=2-3 strings."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._smart_llm(0.1),
            allow_delegation=False,
            cache=False,
        )

        self.qa_critic = Agent(
            role="Analysis Quality Critic",
            goal=(
                "Rate how well the analysis answered the original question. "
                'Output ONLY: {"score": <int 1-10>, "verdict": "<1-2 sentences>"}'
            ),
            backstory=(
                "You review analyses for completeness, specificity, and evidence quality. "
                "Score 10 = every aspect answered with data. Score <5 = question not answered. "
                "Output ONLY the raw JSON — no markdown, no preamble."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._fast_llm(0.2, max_tokens=512),
            allow_delegation=False,
            cache=False,
        )

    def create_tasks(self):
        self.interpret_task = Task(
            description=(
                f"The user wants: {self._ctx}\n\n"
                "Rewrite this into a structured analysis directive:\n"
                "1. The single core question to answer\n"
                "2. Relevant columns/metrics\n"
                "3. Analysis type (trend, comparison, anomaly, summary, correlation)\n"
                "4. Any constraints (date range, thresholds, focus areas)\n\n"
                "Write 3-5 plain sentences addressed directly to a data analyst."
            ),
            expected_output=(
                "3-5 plain sentences. No headers, no bullets. "
                "Direct instruction specifying: the question, relevant columns, analysis type, constraints."
            ),
            agent=self.context_agent,
        )

        self.clean_task = Task(
            description=(
                "Dataset path(s):\n{data}\n\n"
                "If {data} is not '(no file)', use FileReadTool to read each path once.\n"
                "Report per file: type/size, column names, row count, missing values, "
                "obvious issues, 2 sample records.\n"
                "If no file: report 'No file provided — analysis uses context only.'\n"
                "Keep under 200 words."
            ),
            expected_output="Concise data quality report under 200 words. Plain prose or bullets. No JSON.",
            context=[self.interpret_task],
            agent=self.data_cleaner,
        )

        self.prompt_task = Task(
            description=(
                f"Original request: {self._ctx}\n\n"
                "Using the directive and data quality report, write a step-by-step analysis prompt:\n"
                "1. Exact columns to load\n"
                "2. Calculations/aggregations to run\n"
                "3. Patterns, outliers, or trends to look for\n"
                "4. Order of approach\n"
                "5. What a complete answer looks like"
            ),
            expected_output=(
                "Numbered step-by-step prompt. Each step specific and actionable. "
                "References exact column names where possible."
            ),
            context=[self.interpret_task, self.clean_task],
            agent=self.prompt_engineer,
        )

        self.analyze_task = Task(
            description=(
                "Dataset path(s):\n{data}\n\n"
                "If file paths are provided (one per line), read each with FileReadTool exactly once. "
                "If multiple files, analyze together. "
                "If no file, answer from the analysis prompt using reasoning.\n\n"
                "Follow every step in the prompt. Read each file only once. Report only what the data shows."
            ),
            expected_output=(
                "Thorough analysis containing:\n"
                "1. Data source summary\n"
                "2. Key statistics (averages, ranges, counts, outliers)\n"
                "3. 3-5 concrete findings that answer the prompt\n"
                "4. 2-3 actionable recommendations"
            ),
            context=[self.prompt_task],
            agent=self.data_analyst,
        )

        self.format_task = Task(
            description=(
                f"Original request: {self._ctx}\n\n"
                "Convert the analyst's findings into a FormattedOutput JSON object.\n\n"
                "OUTPUT TYPE PRIORITY (pick first that fits):\n"
                "  'code'       → answer is or includes runnable code/queries/scripts\n"
                "  'metrics'    → answer is a set of KPIs or key numbers (3-8 items)\n"
                "  'comparison' → comparing two named entities across multiple metrics;\n"
                "                 set comparison_a_label, comparison_b_label, comparison_rows\n"
                "                 (each row: metric, value_a, value_b, winner='a'/'b'/'tie')\n"
                "  'heatmap'    → data is a matrix (rows × cols) of numeric values;\n"
                "                 set heatmap_row_labels, heatmap_col_labels,\n"
                "                 heatmap_values (2D float list), heatmap_title. Max 10×10.\n"
                "  'table'      → ranked/multi-attribute list, max 20 rows\n"
                "  'chart'      → visual comparison of 2+ values; chart_type options:\n"
                "                 bar, line, pie, scatter, funnel, radar\n"
                "                 For funnel: stages in order, value = count/rate at each stage\n"
                "                 For radar: label=axis, value=series A; optionally value2=series B\n"
                "                            and set radar_b_label for B's name\n"
                "  'report'     → qualitative/narrative findings\n\n"
                "ALWAYS: summary (2-3 sentences), findings (3-5 strings), recommendations (2-3 strings).\n"
                "Return ONLY the raw JSON object. No markdown, no commentary."
            ),
            expected_output=(
                "Single raw JSON object matching FormattedOutput. "
                "No markdown fences. Parseable by json.loads() without modification."
            ),
            context=[self.analyze_task],
            agent=self.output_formatter,
            output_pydantic=FormattedOutput,
        )

        self.qa_task = Task(
            description=(
                f"Original request: {self._ctx}\n\n"
                "Review the completed analysis. Score 1-10 based on:\n"
                "- Did it directly and specifically answer the original question?\n"
                "- Are findings backed by concrete numbers from the data?\n"
                "- Are recommendations actionable and relevant?\n"
                "- Is anything important missing, vague, or invented?\n\n"
                "Return ONLY: "
                '{"score": <int 1-10>, "verdict": "<1-2 sentences: what was done well and what gap remains>"}'
            ),
            expected_output='Raw JSON only: {"score": <int>, "verdict": "<string>"}. No markdown.',
            context=[self.analyze_task, self.format_task],
            agent=self.qa_critic,
        )

    def create_crew(self, data) -> str:
        with _crew_lock:
            return self._run_pipeline(data)

    def _run_pipeline(self, data) -> str:
        max_attempts = (len(_FAST_MODELS) + len(_SMART_MODELS)) * 2

        for attempt in range(max_attempts):
            _wait_until_available()
            fast_model, self._fast_idx = _pick_model(_FAST_MODELS)
            smart_model, self._smart_idx = _pick_model(_SMART_MODELS)

            self.create_agents()
            self.create_tasks()

            crew = Crew(
                agents=[
                    self.context_agent, self.data_cleaner, self.prompt_engineer,
                    self.data_analyst, self.output_formatter, self.qa_critic,
                ],
                tasks=[
                    self.interpret_task, self.clean_task, self.prompt_task,
                    self.analyze_task, self.format_task, self.qa_task,
                ],
                process=Process.sequential,
                verbose=True,
                memory=False,
            )
            try:
                result = crew.kickoff(inputs={"data": data})
            except Exception as e:
                err_str = str(e)
                is_404 = "404" in err_str
                is_rate_limit = any(x in err_str for x in ("429", "RateLimitError", "rate_limit_exceeded"))
                is_bad_request = any(x in err_str for x in ("BadRequestError", "invalid_request_error"))
                is_server_err = any(c in err_str for c in ("402", "401", "503", "529"))
                is_rotatable = is_404 or is_rate_limit or is_bad_request or is_server_err

                if is_rotatable and attempt < max_attempts - 1:
                    if is_rate_limit:
                        cooldown_s = _parse_retry_after(err_str)
                        _set_cooldown(fast_model, cooldown_s)
                        _set_cooldown(smart_model, cooldown_s)
                        print(f"[RATE-LIMIT] fast={fast_model} smart={smart_model} → {cooldown_s:.0f}s cooldown")
                    elif is_404 or is_bad_request:
                        _set_cooldown(fast_model, 600)
                        _set_cooldown(smart_model, 600)
                        print(f"[ROTATE] fast={fast_model} smart={smart_model} → unavailable, 10-min cooldown")
                    else:
                        _set_cooldown(fast_model, 60)
                        _set_cooldown(smart_model, 60)
                        print(f"[SERVER-ERR] fast={fast_model} smart={smart_model} → 60s cooldown")
                    continue
                raise

            fmt_task_out = crew.tasks[4].output if len(crew.tasks) > 4 else None
            formatted = None
            if fmt_task_out:
                if getattr(fmt_task_out, "pydantic", None):
                    formatted = fmt_task_out.pydantic
                else:
                    try:
                        formatted = FormattedOutput(
                            **_json.loads(_extract_json(fmt_task_out.raw or ""))
                        )
                    except Exception:
                        pass

            if formatted:
                qa_raw = result.raw if hasattr(result, "raw") else str(result)
                try:
                    qa = _json.loads(_extract_json(qa_raw))
                    formatted.quality_score = int(qa.get("score", 0)) or None
                    formatted.quality_verdict = qa.get("verdict")
                except Exception:
                    pass
                return formatted.model_dump_json()

            raw = result.raw if hasattr(result, "raw") else str(result)
            return _extract_json(raw)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="DataFlow AI")

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:4173",
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
BOX_CHARS = re.compile(r"[╭╮╰╯│╞╡╢╟╔╗╚╝╬═─┼┤├┬┴┌└┐┘╠╣╦╧╨╩╪╫]")

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_CONTEXT_LEN = 2000
MAX_RUNTIME = 600
MAX_FILES = 3
ALLOWED_EXTS = {".csv", ".json", ".txt", ".pdf", ".xml"}


class LineCapture(io.TextIOBase):
    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text: str) -> int:
        cleaned = ANSI_ESCAPE.sub("", text)
        cleaned = BOX_CHARS.sub("", cleaned)
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
async def analyze(
    context: str = Form(...),
    files: _List[UploadFile] = File(default=[]),
):
    context = context.strip()
    if not context:
        return JSONResponse({"error": "Context is required."}, status_code=400)
    if len(context) > MAX_CONTEXT_LEN:
        return JSONResponse(
            {"error": f"Context too long ({len(context)} chars, max {MAX_CONTEXT_LEN})."},
            status_code=400,
        )

    uploads = [f for f in (files or []) if f and f.filename]
    if len(uploads) > MAX_FILES:
        return JSONResponse({"error": f"Too many files (max {MAX_FILES})."}, status_code=400)

    validated_files = []
    for f in uploads:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            return JSONResponse(
                {"error": f"File '{f.filename}': type '{ext}' not supported. Allowed: {', '.join(sorted(ALLOWED_EXTS))}"},
                status_code=400,
            )
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            return JSONResponse(
                {"error": f"File '{f.filename}' too large ({len(content) // 1024}KB, max {MAX_FILE_SIZE // 1024 // 1024}MB)."},
                status_code=400,
            )
        validated_files.append((content, ext))

    async def event_stream():
        q: queue.Queue = queue.Queue()

        def run_crew():
            old_stdout, old_stderr = sys.stdout, sys.stderr
            capture = LineCapture(q)
            sys.stdout = capture
            sys.stderr = capture
            tmp_paths = []
            try:
                for content, ext in validated_files:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                        tmp.write(content)
                        tmp_paths.append(tmp.name)
                data_arg = "\n".join(tmp_paths) if tmp_paths else "(no file)"
                bots = Bots(context)
                result = bots.create_crew(data_arg)
                if result:
                    q.put({"__result__": result})
            except Exception as exc:
                capture.flush()
                import traceback
                q.put(f"[ERROR] {exc}")
                for line in traceback.format_exc().splitlines():
                    if line.strip():
                        q.put(f"[TRACE] {line}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                for p in tmp_paths:
                    if os.path.exists(p):
                        os.unlink(p)
                q.put(None)

        thread = threading.Thread(target=run_crew, daemon=True)
        thread.start()

        _NOISE = (
            "ERROR:root:",
            "ERROR:crewai.",
            "[CrewAIEventsBus]",
            "An unknown error occurred. Please check",
            "Error details: Error code:",
            "Error details: Model ",
            "'agent_execution_started'",
            "Tracing Preference Saved",
            "Tracing has been disabled",
            "Your preference has been saved",
            "To enable tracing later",
            "Set tracing=True",
            "Set CREWAI_TRACING_ENABLED",
            "Run: crewai traces",
            "[Finalize]",
            "[TRACE]",
            "✨ Update Available",
            "collect traces.",
            "New version of crewai",
            "Run `pip install",
            "pip install --upgrade",
            "All providers rate-limited",
            "Auto-retrying in",
            "[RETRY]",
        )

        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + MAX_RUNTIME

        while True:
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
                yield ": ping\n\n"
                continue

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
