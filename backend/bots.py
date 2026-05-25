from crewai import Agent, Crew, Task, Process, LLM
from crewai_tools import FileReadTool, CSVSearchTool, JSONSearchTool, PDFSearchTool, XMLSearchTool, TXTSearchTool
import os
import time as _time
from threading import Lock
from typing import Optional, Literal
from pydantic import BaseModel

import re as _re
import json as _json
import litellm
litellm.cache = None        # Disable response caching
litellm.drop_params = True  # Silently drop unsupported params per provider

# Groq rejects messages that contain a 'cache_breakpoint' property.
# CrewAI's prompt-caching feature injects it into system message dicts before
# calling litellm.completion — setting litellm.cache=None doesn't prevent this.
# Patch litellm.completion to strip the field from every message at call time.
_real_completion = litellm.completion

def _completion_no_cache_breakpoint(*args, **kwargs):
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
# Tier 1 (Groq): 14,400 req/day free, fast, tried first.
# Tier 2 (OpenRouter :free): shared global rate limits — deep fallback only.
_FAST_MODELS = [
    # ── Tier 1: Groq ──────────────────────────────────────────────────────────
    "groq/llama-3.1-8b-instant",                                   # Groq — primary
    "groq/gemma2-9b-it",                                           # Groq
    "groq/llama3-8b-8192",                                         # Groq — stable fallback
    # ── Tier 2: OpenRouter free fallback ──────────────────────────────────────
    "openrouter/nvidia/nemotron-nano-9b-v2:free",                  # NVIDIA
    "openrouter/minimax/minimax-m2.5:free",                        # OpenInference
    "openrouter/meta-llama/llama-3.1-8b-instruct:free",            # Meta/Lepton
    "openrouter/mistralai/mistral-7b-instruct:free",               # Mistral
    "openrouter/google/gemma-3-12b-it:free",                       # Google
    "openrouter/qwen/qwen3-8b:free",                               # Qwen small
    "openrouter/meta-llama/llama-4-scout:free",                    # Meta Llama 4
    "openrouter/microsoft/phi-3-mini-128k-instruct:free",          # Microsoft
]
_SMART_MODELS = [
    # ── Tier 1: Groq ──────────────────────────────────────────────────────────
    "groq/llama-3.3-70b-versatile",                                # Groq — best tool use
    "groq/llama3-70b-8192",                                        # Groq — stable 70b fallback
    # ── Tier 2: OpenRouter free fallback ──────────────────────────────────────
    "openrouter/google/gemma-3-27b-it:free",                       # Google
    "openrouter/qwen/qwen3-coder:free",                            # Qwen
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",           # Meta
    "openrouter/deepseek/deepseek-chat-v3-0324:free",              # DeepSeek
    "openrouter/meta-llama/llama-4-maverick:free",                 # Meta Llama 4
    "openrouter/mistralai/mistral-small-3.1-24b-instruct:free",    # Mistral
    "openrouter/google/gemma-3-12b-it:free",                       # Google smaller
]


# ── Module-level cooldown state (persists across requests) ───────────────────
# Groq models share one org-wide TPM quota — rate-limiting one limits all.
_GROQ_MODELS_ALL: frozenset = frozenset(
    m for m in _FAST_MODELS + _SMART_MODELS if m.startswith("groq/")
)
_cooldown: dict[str, float] = {}   # model → monotonic timestamp when available again
_cooldown_lock = Lock()


def _set_cooldown(model: str, seconds: float) -> None:
    """Mark model unavailable. Groq models cool together (shared org TPM quota)."""
    until = _time.monotonic() + seconds
    with _cooldown_lock:
        if model.startswith("groq/"):
            for m in _GROQ_MODELS_ALL:
                _cooldown[m] = max(_cooldown.get(m, 0.0), until)
        else:
            _cooldown[model] = max(_cooldown.get(model, 0.0), until)


def _pick_model(pool: list[str]) -> tuple[str, int]:
    """Return (model, index) of the highest-priority model not in cooldown.
    Always scans from index 0 so higher-priority models (Groq) are preferred
    the moment their cooldown expires. Falls back to soonest-available if all
    are still cooling."""
    now = _time.monotonic()
    with _cooldown_lock:
        for idx, model in enumerate(pool):
            if _cooldown.get(model, 0.0) <= now:
                return model, idx
        # All cooling — return the one whose cooldown expires soonest
        best = min(range(len(pool)), key=lambda i: _cooldown.get(pool[i], 0.0))
        return pool[best], best


def _wait_until_available() -> None:
    """Sleep until at least one model in each pool is ready. No-op if already ready."""
    now = _time.monotonic()
    with _cooldown_lock:
        fast_waits = [max(0.0, _cooldown.get(m, 0.0) - now) for m in _FAST_MODELS]
        smart_waits = [max(0.0, _cooldown.get(m, 0.0) - now) for m in _SMART_MODELS]
    sleep_s = max(min(fast_waits), min(smart_waits))
    if sleep_s > 0:
        print(f"[WAIT] All providers cooling — resuming in {sleep_s:.0f}s")
        _time.sleep(sleep_s)


def _parse_retry_after(err_str: str) -> float:
    """Extract retry delay from provider error string, default 35s.

    Handles two formats:
    - OpenRouter: retry_after_seconds: 30
    - Groq:       Please try again in 2.3s.
    """
    m = _re.search(r"retry_after_seconds['\"\s:]+(\d+(?:\.\d+)?)", err_str)
    if m:
        return float(m.group(1)) + 5
    m = _re.search(r"[Pp]lease try again in (\d+(?:\.\d+)?)s", err_str)
    if m:
        return float(m.group(1)) + 2
    return 35.0


def _extract_json(text: str) -> str:
    """
    Robustly extract a JSON object from LLM output.

    LLMs often wrap output in markdown fences (```json...```) despite instructions.
    This strips fences first, then uses brace-counting to find the first complete
    JSON object — avoiding greedy regex that matches across multiple objects.
    """
    # Strip markdown fences
    text = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.MULTILINE)
    text = _re.sub(r"\s*```$", "", text.strip(), flags=_re.MULTILINE)
    text = text.strip()

    # Try parsing the cleaned text directly
    try:
        _json.loads(text)
        return text
    except _json.JSONDecodeError:
        pass

    # Walk characters counting braces to find the first balanced JSON object
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
                    candidate = text[start : i + 1]
                    try:
                        _json.loads(candidate)
                        return candidate
                    except _json.JSONDecodeError:
                        break

    return text


# LiteLLM reads provider keys from environment automatically for known prefixes.
# Mirror the OpenRouter key to OPENAI_API_KEY so LiteLLM internal paths that
# fall back to OpenAI don't error when no OpenAI key is set.
_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
if _OR_KEY:
    os.environ.setdefault("OPENAI_API_KEY", _OR_KEY)


def _api_key_for(model: str) -> str | None:
    """Return the correct API key for a given model string."""
    if model.startswith("groq/"):
        return os.getenv("GROQ_API_KEY")
    return os.getenv("OPENROUTER_API_KEY")


# ── Output schemas ────────────────────────────────────────────────────────────

class DataPoint(BaseModel):
    label: str
    value: float
    category: Optional[str] = None


class FormattedOutput(BaseModel):
    """
    Strict output contract for the output_formatter agent.

    RULES:
    - output_type = "chart" when the findings contain numerical data that can
      be meaningfully compared across discrete categories or over time.
      Examples: revenue by product, sessions by user, requests by endpoint,
      errors by day.
    - output_type = "report" when the findings are qualitative, narrative,
      or mixed — no clear numerical comparison is possible.

    CHART TYPE RULES (only when output_type = "chart"):
    - chart_type = "bar"  → comparing discrete categories (products, regions,
      users, endpoints). Most common choice.
    - chart_type = "line" → trend over time (daily counts, weekly averages).
      Use when labels are dates or sequential time periods.
    - chart_type = "pie"  → parts of a whole where values sum to a meaningful
      total and there are 2-6 categories. Do NOT use for more than 6 slices.

    DATA RULES:
    - data_points must be populated whenever output_type = "chart".
    - label: the category name (short, no line breaks).
    - value: the primary numeric metric (revenue, count, duration, etc.).
    - category: optional secondary grouping (e.g. "Hardware", "Software").
    - x_axis_label: what the labels represent (e.g. "Product", "Date", "Region").
    - y_axis_label: what the values represent (e.g. "Revenue ($)", "Sessions").

    REPORT FIELDS (always required):
    - summary: 2-3 sentences answering the user's original question directly.
    - findings: exactly 3-5 bullet-ready strings, each a concrete fact from data.
    - recommendations: exactly 2-3 actionable strings based solely on the data.
    """
    output_type: Literal["chart", "report"]
    chart_type: Optional[Literal["bar", "line", "pie"]] = None
    chart_title: Optional[str] = None
    x_axis_label: Optional[str] = None
    y_axis_label: Optional[str] = None
    data_points: Optional[list[DataPoint]] = None
    summary: str
    findings: list[str]
    recommendations: list[str]


# ── Bots ──────────────────────────────────────────────────────────────────────

class Bots:
    def __init__(self, context: str):
        self.context = context
        # Escape braces so CrewAI's .format() interpolation doesn't treat user
        # text like "{something}" as a template variable and raise KeyError.
        self._ctx = context.replace("{", "{{").replace("}", "}}")
        self._fast_idx = 0
        self._smart_idx = 0
        self.file_read = FileReadTool()
        self.csv_search = CSVSearchTool()
        self.json_search = JSONSearchTool()
        self.pdf_search = PDFSearchTool()
        self.xml_search = XMLSearchTool()
        self.txt_search = TXTSearchTool()

    def _smart_llm(self, temperature: float) -> LLM:
        model = _SMART_MODELS[self._smart_idx % len(_SMART_MODELS)]
        return LLM(
            model=model,
            api_key=_api_key_for(model),
            max_tokens=4096,
            max_retries=0,
            timeout=120,
            temperature=temperature,
        )

    def _fast_llm(self, temperature: float) -> LLM:
        model = _FAST_MODELS[self._fast_idx % len(_FAST_MODELS)]
        return LLM(
            model=model,
            api_key=_api_key_for(model),
            max_tokens=1024,
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
                "or metrics, and the exact type of analysis needed (trend, comparison, "
                "anomaly, summary, correlation). Output only the directive — nothing else."
            ),
            backstory=(
                "You are an expert at translating vague or freeform requests into sharp, "
                "actionable instructions for data analysts. You have a talent for identifying "
                "what someone actually wants to know versus what they literally said. "
                "You never perform analysis yourself — your only job is to make the analyst's "
                "directive so clear that there is no room for misinterpretation."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._fast_llm(0.2),
            allow_delegation=False,
            cache=False,
        )

        self.prompt_engineer = Agent(
            role="Data Analysis Prompt Engineer",
            goal=(
                "Take the raw FastAPI input and the analysis directive, then construct a "
                "precise, step-by-step analysis prompt for the data analyst. The prompt must "
                "specify exactly which columns to examine, what calculations to run, what "
                "patterns to look for, and in what order to approach the analysis."
            ),
            backstory=(
                "You are a specialist in writing technical prompts for data analysis pipelines. "
                "You understand how LLM-based analysts think and know that vague instructions "
                "produce vague results. You break every analysis job into clear, ordered steps "
                "with explicit column names, metric names, and success criteria. You never "
                "perform the analysis yourself — you only write the instruction that makes it happen."
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
                "Follow the analysis prompt exactly. If a file path is provided, use the correct "
                "tool for that file type and extract findings that directly answer the prompt. "
                "If no file is provided, reason from the context and prompt alone. "
                "Never speculate beyond what the data or context shows."
            ),
            backstory=(
                "You are a rigorous data analyst with experience across many domains and file formats. "
                "You always select the right tool for the file type: CSVSearchTool for .csv, "
                "JSONSearchTool for .json, PDFSearchTool for .pdf, XMLSearchTool for .xml, "
                "TXTSearchTool for .txt, and FileReadTool for any other file type. "
                "When no file is provided, you provide a thorough analytical response based on "
                "the context and analysis prompt. You back every finding with evidence."
            ),
            tools=[
                self.file_read,
                self.csv_search,
                self.json_search,
                self.pdf_search,
                self.xml_search,
                self.txt_search,
            ],
            verbose=True,
            memory=False,
            llm=self._smart_llm(0.1),
            allow_delegation=False,
            cache=False,
        )

        self.output_formatter = Agent(
            role="Structured Output Specialist",
            goal=(
                "Convert the analyst's findings into a strict FormattedOutput JSON object. "
                "Decide output_type based on one rule: if there are numerical values that can "
                "be meaningfully compared across 2 or more categories, use 'chart'. Otherwise "
                "use 'report'. Never invent data — only use what the analyst found."
            ),
            backstory=(
                "You are an expert in structured data serialization. You always output valid "
                "JSON matching the FormattedOutput schema exactly — no extra keys, no missing "
                "required fields, no markdown fences around the JSON. "
                "Your chart selection rules are strict: "
                "bar for category comparisons, line for time-series trends, pie only for "
                "2-6 slices that sum to a whole. You populate data_points with the actual "
                "numbers from the analyst's report, converted to floats. "
                "You always write summary in plain English (2-3 sentences), "
                "findings as 3-5 specific factual strings, and recommendations as 2-3 "
                "actionable strings. You never add commentary outside the JSON object."
            ),
            tools=[],
            verbose=True,
            memory=False,
            llm=self._smart_llm(0.1),
            allow_delegation=False,
            cache=False,
        )

    def create_tasks(self):
        self.interpret_task = Task(
            description=(
                f"The user has provided this context about what they want analyzed:\n\n"
                f"CONTEXT: {self._ctx}\n\n"
                "Rewrite this into a structured analysis directive by answering these four questions:\n"
                "1. What is the single core question to answer?\n"
                "2. Which columns or metrics are most relevant to that question?\n"
                "3. What analysis type is needed — trend over time, comparison between groups, "
                "anomaly detection, statistical summary, or correlation?\n"
                "4. Are there any constraints or focus areas implied by the context "
                "(e.g. a date range, a specific user, a threshold)?\n\n"
                "Write the final directive as 3-5 plain sentences addressed directly to a data analyst."
            ),
            expected_output=(
                "A single block of 3-5 plain sentences. No headers, no bullet points, no preamble. "
                "Written as a direct instruction to a data analyst. Must specify: the question to answer, "
                "the relevant columns, the analysis type, and any constraints."
            ),
            agent=self.context_agent,
        )

        self.prompt_task = Task(
            description=(
                f"You have received the original user request and an analysis directive.\n\n"
                f"ORIGINAL REQUEST: {self._ctx}\n\n"
                "Using the directive from the previous step, write a precise step-by-step "
                "analysis prompt for the data analyst. Your prompt must include:\n"
                "1. The exact columns to load and examine\n"
                "2. The specific calculations or aggregations to run\n"
                "3. What patterns, outliers, or trends to look for\n"
                "4. The order in which to approach the analysis\n"
                "5. What a complete, correct answer looks like"
            ),
            expected_output=(
                "A numbered step-by-step prompt addressed to a data analyst. "
                "Each step must be specific and actionable — no vague instructions. "
                "Must reference exact column names or metric types where possible."
            ),
            context=[self.interpret_task],
            agent=self.prompt_engineer,
        )

        self.analyze_task = Task(
            description=(
                "You have been given a step-by-step analysis prompt from the previous task.\n\n"
                "Dataset path: {data}\n\n"
                "If {data} is not empty, check the file extension and use the correct tool:\n"
                "- .csv  → CSVSearchTool\n"
                "- .json → JSONSearchTool\n"
                "- .pdf  → PDFSearchTool\n"
                "- .xml  → XMLSearchTool\n"
                "- .txt  → TXTSearchTool\n"
                "- anything else → FileReadTool\n\n"
                "If {data} is empty, answer based on the analysis prompt using your knowledge "
                "and reasoning — state clearly that no file was provided.\n\n"
                "Follow every step in the prompt exactly. Report only what the data shows."
            ),
            expected_output=(
                "A thorough data analysis report containing:\n"
                "1. Data source summary (file type, rows found, or 'no file provided')\n"
                "2. Key statistics relevant to the prompt (averages, ranges, counts, outliers)\n"
                "3. 3-5 concrete findings that directly answer the prompt\n"
                "4. 2-3 actionable recommendations based on the findings"
            ),
            context=[self.prompt_task],
            agent=self.data_analyst,
        )

        self.format_task = Task(
            description=(
                f"Convert the analyst's findings into a FormattedOutput JSON object.\n\n"
                f"ORIGINAL USER REQUEST: {self._ctx}\n\n"
                "DECISION RULE — output_type:\n"
                "  'chart' if the findings contain at least 2 data points with distinct numerical "
                "values that can be meaningfully compared (revenue by product, sessions by user, "
                "requests by endpoint, errors by day, etc.).\n"
                "  'report' for qualitative, narrative, or text-heavy findings.\n\n"
                "CHART TYPE SELECTION:\n"
                "  bar   → comparing named categories (products, regions, users, endpoints)\n"
                "  line  → trend over sequential time periods (days, weeks, months)\n"
                "  pie   → parts of a whole, 2-6 categories only\n\n"
                "OUTPUT REQUIREMENTS:\n"
                "- Return ONLY the raw JSON object — no markdown fences, no commentary.\n"
                "- data_points: extract actual numbers from the analyst's report as floats.\n"
                "- summary: 2-3 sentences answering the original request directly.\n"
                "- findings: 3-5 specific factual strings (not bullet points, just the text).\n"
                "- recommendations: 2-3 actionable strings.\n"
                "- chart_title, x_axis_label, y_axis_label: short, descriptive strings.\n"
                "- If output_type is 'report', set chart_type, chart_title, x_axis_label, "
                "y_axis_label, and data_points to null."
            ),
            expected_output=(
                "A single raw JSON object matching the FormattedOutput schema. "
                "No markdown fences, no preamble, no trailing text. "
                "The JSON must be parseable by json.loads() without modification."
            ),
            context=[self.analyze_task],
            agent=self.output_formatter,
            output_pydantic=FormattedOutput,
        )

    def create_crew(self, data) -> str:
        max_attempts = (len(_FAST_MODELS) + len(_SMART_MODELS)) * 2

        for attempt in range(max_attempts):
            # Scan from index 0 every time so Groq (index 0) is always preferred
            # when its cooldown has expired. Cooldowns handle skipping, not the index.
            fast_model, self._fast_idx = _pick_model(_FAST_MODELS)
            smart_model, self._smart_idx = _pick_model(_SMART_MODELS)

            self.create_agents()
            self.create_tasks()

            crew = Crew(
                agents=[self.context_agent, self.prompt_engineer, self.data_analyst, self.output_formatter],
                tasks=[self.interpret_task, self.prompt_task, self.analyze_task, self.format_task],
                process=Process.sequential,
                verbose=True,
                memory=False,
            )
            try:
                result = crew.kickoff(inputs={"data": data})
            except Exception as e:
                err_str = str(e)
                is_404 = "404" in err_str
                is_rate_limit = "429" in err_str
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

                    # If every model in both pools is still cooling, sleep until ready.
                    # Cooldowns already prevent re-picking failed models — no index advance needed.
                    _wait_until_available()
                    continue
                raise

            # Best case: pydantic model validated successfully
            if hasattr(result, "pydantic") and result.pydantic is not None:
                return result.pydantic.model_dump_json()

            raw = result.raw if hasattr(result, "raw") else str(result)
            return _extract_json(raw)
