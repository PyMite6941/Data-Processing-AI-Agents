from crewai import Agent, Crew, Task, Process, LLM
from crewai_tools import FileReadTool, CSVSearchTool, JSONSearchTool, PDFSearchTool, XMLSearchTool, TXTSearchTool
import os
from typing import Optional, Literal
from pydantic import BaseModel


# ── Output schemas ────────────────────────────────────────────────────────────

class Report(BaseModel):
    summary: str
    key_findings: list[str]
    anomalies: list[str]
    recommendations: list[str]


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
        self._smart_config = dict(
            model="openrouter/deepseek/deepseek-r1:free",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            max_tokens=4096,
            max_retries=2,
            timeout=30,
        )
        self._fast_config = dict(
            model="openrouter/groq/groq-1:free",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            max_tokens=4096,
            max_retries=2,
            timeout=30,
        )
        self.file_read = FileReadTool()
        self.csv_search = CSVSearchTool()
        self.json_search = JSONSearchTool()
        self.pdf_search = PDFSearchTool()
        self.xml_search = XMLSearchTool()
        self.txt_search = TXTSearchTool()

    def _smart_llm(self, temperature: float) -> LLM:
        return LLM(**self._smart_config, temperature=temperature)

    def _fast_llm(self, temperature: float) -> LLM:
        return LLM(**self._fast_config, temperature=temperature)

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
            cache=True,
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
            cache=True,
        )

        self.data_analyst = Agent(
            role="Senior Data Analyst",
            goal=(
                "Follow the analysis prompt exactly. Use the correct tool for the file type "
                "provided, then extract only the findings that directly answer the prompt. "
                "Never speculate beyond what the data shows."
            ),
            backstory=(
                "You are a rigorous data analyst with experience across many domains and file formats. "
                "You always select the right tool for the file type: CSVSearchTool for .csv, "
                "JSONSearchTool for .json, PDFSearchTool for .pdf, XMLSearchTool for .xml, "
                "TXTSearchTool for .txt, and FileReadTool for any other file type. "
                "You back every finding with statistics and never go beyond the scope of your prompt."
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
            memory=True,
            llm=self._smart_llm(0.1),
            max_rpm=15,
            allow_delegation=False,
            cache=True,
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
            cache=True,
        )

    def create_tasks(self):
        self.interpret_task = Task(
            description=(
                f"The user has provided this context about what they want analyzed:\n\n"
                f"CONTEXT: {self.context}\n\n"
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
                f"ORIGINAL REQUEST: {self.context}\n\n"
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
                "You have been given a dataset at path {data} and a step-by-step analysis prompt.\n\n"
                "First, check the file extension of {data} and select the correct tool:\n"
                "- .csv  → CSVSearchTool\n"
                "- .json → JSONSearchTool\n"
                "- .pdf  → PDFSearchTool\n"
                "- .xml  → XMLSearchTool\n"
                "- .txt  → TXTSearchTool\n"
                "- anything else → FileReadTool\n\n"
                "Then follow every step in the prompt from the previous task exactly. "
                "Report only what the data shows."
            ),
            expected_output=(
                "A structured data analysis report containing:\n"
                "1. Data quality summary (rows found, nulls or missing values noted)\n"
                "2. Key statistics relevant to the prompt (averages, ranges, outliers)\n"
                "3. 3-5 findings that directly answer the prompt\n"
                "4. 2-3 actionable recommendations based solely on the data"
            ),
            context=[self.prompt_task],
            agent=self.data_analyst,
            output_pydantic=Report,
        )

        self.format_task = Task(
            description=(
                f"Convert the analyst's findings into a FormattedOutput JSON object.\n\n"
                f"ORIGINAL USER REQUEST: {self.context}\n\n"
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
        self.crew = Crew(
            agents=[self.context_agent, self.prompt_engineer, self.data_analyst, self.output_formatter],
            tasks=[self.interpret_task, self.prompt_task, self.analyze_task, self.format_task],
            process=Process.sequential,
            verbose=True,
            memory=True,
            embedder={
                "provider": "fastembed",
                "config": {
                    "model": "BAAI/bge-small-en-v1.5",
                }
            },
        )
        result = self.crew.kickoff(inputs={"data": data})
        # Prefer the validated pydantic model — guaranteed to match FormattedOutput
        if hasattr(result, "pydantic") and result.pydantic is not None:
            return result.pydantic.model_dump_json()
        return str(result)
