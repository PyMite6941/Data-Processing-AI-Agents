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
import re as _re
import time as _time
import asyncio
import json
import json as _json
from threading import Lock
from typing import Optional, Literal, List as _List

# ── Third-party ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel

from crewai import Agent, Crew, Task, Process, LLM
from crewai_tools import FileReadTool

import litellm
litellm.cache = None
litellm.drop_params = True

# ── Groq cache_breakpoint patch ───────────────────────────────────────────────
# Groq rejects messages that contain a 'cache_breakpoint' property.
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
    "groq/llama-3.1-8b-instant",
    "groq/gemma2-9b-it",
    "groq/llama3-8b-8192",
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
    "groq/llama-3.3-70b-versatile",
    "groq/llama3-70b-8192",
    "openrouter/google/gemma-3-27b-it:free",
    "openrouter/qwen/qwen3-coder:free",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/deepseek/deepseek-chat-v3-0324:free",
    "openrouter/meta-llama/llama-4-maverick:free",
    "openrouter/mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/google/gemma-3-12b-it:free",
]

# ── Cooldown state ────────────────────────────────────────────────────────────
_GROQ_MODELS_ALL: frozenset = frozenset(
    m for m in _FAST_MODELS + _SMART_MODELS if m.startswith("groq/")
)
_cooldown: dict[str, float] = {}
_cooldown_lock = Lock()
_crew_lock = Lock()


def _set_cooldown(model: str, seconds: float) -> None:
    until = _time.monotonic() + seconds
    with _cooldown_lock:
        if model.startswith("groq/"):
            for m in _GROQ_MODELS_ALL:
                _cooldown[m] = max(_cooldown.get(m, 0.0), until)
        else:
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


def _api_key_for(model: str) -> str | None:
    if model.startswith("groq/"):
        return os.getenv("GROQ_API_KEY")
    return os.getenv("OPENROUTER_API_KEY")


# ── Output schemas ────────────────────────────────────────────────────────────

class DataPoint(BaseModel):
    label: str
    value: float
    category: Optional[str] = None
    x_value: Optional[float] = None   # scatter: second axis
    value2: Optional[float] = None    # radar: second series value


class CodeBlock(BaseModel):
    language: str    # python | sql | bash | r | javascript
    title: str
    code: str        # keep under 400 chars


class MetricItem(BaseModel):
    label: str
    value: str       # formatted string, e.g. "1,234" or "98.5%"
    unit: Optional[str] = None
    trend: Optional[str] = None    # up | down | neutral
    change: Optional[str] = None   # e.g. "+12%"
    context: Optional[str] = None  # e.g. "vs last week"


class ComparisonRow(BaseModel):
    metric: str          # e.g. "Avg Revenue"
    value_a: str         # formatted value for entity A
    value_b: str         # formatted value for entity B
    winner: Optional[Literal["a", "b", "tie"]] = None


class FormattedOutput(BaseModel):
    """
    OUTPUT TYPE DECISION RULES — pick the FIRST that matches:
    1. "code"       → answer is or includes runnable code/queries/scripts.
                      Populate code_blocks (1-3 blocks).
    2. "metrics"    → answer is a set of KPIs or key numbers (3-8 items).
                      Populate metrics list.
    3. "comparison" → comparing two named entities side-by-side across metrics.
                      Populate comparison_a_label, comparison_b_label, comparison_rows.
    4. "heatmap"    → data is a matrix (rows × columns) of numeric values — e.g.
                      a correlation matrix, time-of-day × day-of-week activity grid,
                      or category × category frequency table.
                      Populate heatmap_row_labels, heatmap_col_labels, heatmap_values.
                      Max 10 rows × 10 cols.
    5. "table"      → ranked/multi-attribute list best shown as labelled rows+columns.
                      Populate table_headers and table_rows (max 20 rows).
    6. "chart"      → 2+ numeric values that can be compared visually.
                      Choose chart_type:
                        bar     → named categories
                        line    → sequential time periods
                        pie     → parts of a whole, 2-6 slices
                        scatter → correlation (set x_value + value per DataPoint)
                        funnel  → sequential stages with drop-off (conversion pipelines)
                        radar   → multi-attribute profile comparison across dimensions;
                                  set value for series A; set value2 + radar_b_label
                                  if comparing two entities on the same axes.
    7. "report"     → qualitative or narrative findings.

    ALWAYS REQUIRED:
    - summary: 2-3 sentences directly answering the original question.
    - findings: 3-5 specific factual strings from the data.
    - recommendations: 2-3 actionable strings.
    - Set unused fields to null.
    """
    output_type: Literal["chart", "report", "code", "table", "metrics", "comparison", "heatmap"]
    # chart
    chart_type: Optional[Literal["bar", "line", "pie", "scatter", "funnel", "radar"]] = None
    chart_title: Optional[str] = None
    x_axis_label: Optional[str] = None
    y_axis_label: Optional[str] = None
    data_points: Optional[list[DataPoint]] = None
    radar_b_label: Optional[str] = None   # label for value2 series in radar
    # code
    code_blocks: Optional[list[CodeBlock]] = None
    # table
    table_headers: Optional[list[str]] = None
    table_rows: Optional[list[list[str]]] = None
    # metrics
    metrics: Optional[list[MetricItem]] = None
    # comparison
    comparison_a_label: Optional[str] = None
    comparison_b_label: Optional[str] = None
    comparison_rows: Optional[list[ComparisonRow]] = None
    # heatmap
    heatmap_title: Optional[str] = None
    heatmap_row_labels: Optional[list[str]] = None
    heatmap_col_labels: Optional[list[str]] = None
    heatmap_values: Optional[list[list[float]]] = None  # [row_idx][col_idx]
    # always
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

    def _smart_llm(self, temperature: float) -> LLM:
        model = _SMART_MODELS[self._smart_idx % len(_SMART_MODELS)]
        return LLM(
            model=model,
            api_key=_api_key_for(model),
            max_tokens=1024,
            max_retries=0,
            timeout=120,
            temperature=temperature,
        )

    def _fast_llm(self, temperature: float, max_tokens: int = 1024) -> LLM:
        model = _FAST_MODELS[self._fast_idx % len(_FAST_MODELS)]
        return LLM(
            model=model,
            api_key=_api_key_for(model),
            max_tokens=max_tokens,
            max_retries=0,
            timeout=120,
            temperature=temperature,
        )

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

