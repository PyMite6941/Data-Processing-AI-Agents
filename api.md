# DataFlow API Reference

**Base URL:** `https://pymite6941-data-analyst-ai-agent.hf.space`

**Health check:** `GET /health` → `{ "status": "ok" }`

---

## Fair use

This is a free public demo API. A few things to know before building on it:

- **No API key required** — anyone can call it.
- **One request runs at a time** — requests queue behind each other (no parallelism).
- **No data is stored** — uploaded files are deleted immediately after analysis.
- **Quota-limited** — the backend runs on free-tier LLM providers. Heavy automated use will exhaust the daily quota for everyone. Please be reasonable.
- **No SLA** — this is a portfolio project. Uptime is best-effort.

---

## POST /analyze

Runs the 6-agent analysis pipeline and streams results back as Server-Sent Events.

### Request

`Content-Type: multipart/form-data`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `context` | string | Yes | Max 2000 chars | Natural-language question or description of what to analyze |
| `files` | File (binary) | No | Up to 3 files, max 10 MB each | Data files to analyze together |

**Accepted file types:** `.csv` `.json` `.txt` `.pdf` `.xml`

**Validation errors** (returned as JSON before the stream opens):

```json
{ "error": "Context is required." }
{ "error": "Context too long (2100 chars, max 2000)." }
{ "error": "Too many files (max 3)." }
{ "error": "File 'data.exe': type '.exe' not supported. Allowed: .csv, .json, .pdf, .txt, .xml" }
{ "error": "File 'huge.csv' too large (15360KB, max 10MB)." }
```

### JavaScript

```js
const form = new FormData()
form.append('context', 'Show me the top 5 products by revenue in Q3')
form.append('files', file1)   // optional — repeat for multiple files
form.append('files', file2)

const res = await fetch('https://pymite6941-data-analyst-ai-agent.hf.space/analyze', {
  method: 'POST',
  body: form,
})
```

### Python

```python
import requests

res = requests.post(
    'https://pymite6941-data-analyst-ai-agent.hf.space/analyze',
    data={'context': 'What are the top error types in this log?'},
    files=[
        ('files', ('server.log', open('server.log', 'rb'))),
        ('files', ('metrics.csv', open('metrics.csv', 'rb'))),  # optional second file
    ],
    stream=True,
)
```

### curl

```bash
curl -N -X POST https://pymite6941-data-analyst-ai-agent.hf.space/analyze \
  -F "context=Show me monthly revenue trends" \
  -F "files=@sales.csv"
```

---

## Response — Server-Sent Events

The response stays open as an SSE stream while the 6-agent pipeline runs (~1–3 min). Every frame has the form:

```
data: <JSON-encoded payload>\n\n
```

Three payload shapes arrive in order:

| Shape | When | Action |
|---|---|---|
| `"some log string"` | Throughout the run | Display in a live feed |
| `{"type": "result", "content": "<json string>"}` | Pipeline complete | Parse `content` as JSON |
| `"__DONE__"` | After result (or on timeout/error) | Close the stream |

A heartbeat comment (`: ping`) fires every 30s on idle to keep the connection alive through proxies.

### Reading loop — JavaScript

```js
const reader = res.body.getReader()
const decoder = new TextDecoder()
let buf = ''

while (true) {
  const { done, value } = await reader.read()
  if (done) break

  buf += decoder.decode(value, { stream: true })
  const lines = buf.split('\n')
  buf = lines.pop()                           // hold incomplete line

  for (const raw of lines) {
    if (!raw.startsWith('data: ')) continue
    const payload = JSON.parse(raw.slice(6))  // strip "data: "

    if (payload === '__DONE__') return
    if (payload?.type === 'result') {
      const result = JSON.parse(payload.content)
      handleResult(result)
    } else {
      appendLog(payload)                      // string log line
    }
  }
}
```

### Reading loop — Python

```python
import json

for raw in res.iter_lines():
    line = raw.decode('utf-8')
    if not line.startswith('data: '):
        continue
    payload = json.loads(line[6:])
    if payload == '__DONE__':
        break
    if isinstance(payload, dict) and payload.get('type') == 'result':
        result = json.loads(payload['content'])
        handle_result(result)
    else:
        print('[LOG]', payload)
```

---

## Result schema

`payload.content` is a JSON string. Parse it to get the result object.

Every result includes these fields regardless of type:

| Field | Type | Description |
|---|---|---|
| `output_type` | string | One of 7 types — see below |
| `summary` | string | 2–3 sentences answering the original question |
| `findings` | string[] | 3–5 specific factual findings from the data |
| `recommendations` | string[] | 2–3 actionable next steps |
| `quality_score` | int \| null | QA Critic's 1–10 rating of the analysis quality |
| `quality_verdict` | string \| null | 1–2 sentence explanation of the score |

The `output_type` field determines which additional fields are populated.

---

### `output_type: "chart"`

Visual comparison of numeric values.

| Field | Type | Notes |
|---|---|---|
| `chart_type` | string | `"bar"` `"line"` `"pie"` `"scatter"` `"funnel"` `"radar"` |
| `chart_title` | string | |
| `x_axis_label` | string \| null | |
| `y_axis_label` | string \| null | |
| `data_points` | DataPoint[] | |
| `radar_b_label` | string \| null | Label for the second radar series (when using `value2`) |

**DataPoint:**
```ts
{
  label:    string          // category name or axis label
  value:    number          // primary value (Y axis, series A)
  category: string | null   // optional grouping
  x_value:  number | null   // scatter: X axis value
  value2:   number | null   // radar: second series value
}
```

**Chart type guide:**
- `bar` — named categories (products, regions, error types)
- `line` — sequential time periods (days, weeks, months)
- `pie` — parts of a whole, 2–6 slices
- `scatter` — correlation; `x_value` and `value` set per point
- `funnel` — sequential stages with drop-off (conversion pipelines)
- `radar` — multi-attribute profile; optionally dual-series with `value2` + `radar_b_label`

```json
{
  "output_type": "chart",
  "chart_type": "bar",
  "chart_title": "Top 5 Products by Revenue — Q3",
  "y_axis_label": "Revenue (USD)",
  "data_points": [
    { "label": "Widget A", "value": 84200, "category": "hardware" },
    { "label": "Widget B", "value": 61500, "category": "hardware" }
  ],
  "summary": "Widget A led Q3 with $84,200 in revenue...",
  "findings": ["Widget A outperformed Q2 by 18%"],
  "recommendations": ["Increase Widget A inventory for Q4"],
  "quality_score": 9,
  "quality_verdict": "All top products identified with exact revenue figures."
}
```

---

### `output_type: "metrics"`

A dashboard of key numbers or KPIs.

| Field | Type |
|---|---|
| `metrics` | MetricItem[] |

**MetricItem:**
```ts
{
  label:   string          // e.g. "Total Requests"
  value:   string          // formatted string: "541,466" / "98.5%" / "$4.2M"
  unit:    string | null   // e.g. "ms" or "req/hr"
  trend:   "up" | "down" | "neutral" | null
  change:  string | null   // e.g. "+12%" or "-3.2"
  context: string | null   // e.g. "vs last week"
}
```

```json
{
  "output_type": "metrics",
  "metrics": [
    { "label": "Avg Latency", "value": "142", "unit": "ms", "trend": "down", "change": "-18%", "context": "vs last week" },
    { "label": "Error Rate",  "value": "0.4%", "trend": "up", "change": "+0.1pp" }
  ],
  "summary": "System performance improved significantly this week...",
  "findings": ["P99 latency dropped from 890ms to 610ms"],
  "recommendations": ["Investigate remaining error spike at 03:00 UTC"]
}
```

---

### `output_type: "code"`

Runnable scripts, queries, or algorithms.

| Field | Type |
|---|---|
| `code_blocks` | CodeBlock[] (1–3 blocks) |

**CodeBlock:**
```ts
{
  language: "python" | "sql" | "bash" | "r" | "javascript"
  title:    string   // short label, e.g. "Filter failed logins"
  code:     string   // runnable code snippet
}
```

```json
{
  "output_type": "code",
  "code_blocks": [
    {
      "language": "python",
      "title": "Load and filter Q3 sales",
      "code": "import pandas as pd\ndf = pd.read_csv('sales.csv')\nq3 = df[df['quarter'] == 'Q3']\ntop5 = q3.groupby('product')['revenue'].sum().nlargest(5)"
    }
  ],
  "summary": "This script loads the sales CSV and extracts Q3 top performers.",
  "findings": ["Widget A leads Q3 at $84,200"],
  "recommendations": ["Run weekly to track ranking shifts"]
}
```

---

### `output_type: "table"`

A labelled grid of rows and columns (up to 20 rows).

| Field | Type |
|---|---|
| `table_headers` | string[] |
| `table_rows` | string[][] (each row same length as headers) |

```json
{
  "output_type": "table",
  "table_headers": ["Product", "Q3 Revenue", "Q2 Revenue", "Growth"],
  "table_rows": [
    ["Widget A", "$84,200", "$71,300", "+18%"],
    ["Widget B", "$61,500", "$58,900", "+4.4%"]
  ],
  "summary": "Five products drove 72% of Q3 revenue.",
  "findings": ["Widget A is the only product with >10% QoQ growth"],
  "recommendations": ["Prioritise Widget A restocking before Q4"]
}
```

---

### `output_type: "comparison"`

Side-by-side comparison of two named entities across multiple metrics.

| Field | Type |
|---|---|
| `comparison_a_label` | string | Name of entity A (e.g. "Q1 2024") |
| `comparison_b_label` | string | Name of entity B (e.g. "Q2 2024") |
| `comparison_rows` | ComparisonRow[] |

**ComparisonRow:**
```ts
{
  metric:  string                       // e.g. "Avg Revenue"
  value_a: string                       // formatted value for A
  value_b: string                       // formatted value for B
  winner:  "a" | "b" | "tie" | null    // which entity wins on this metric
}
```

```json
{
  "output_type": "comparison",
  "comparison_a_label": "Model A",
  "comparison_b_label": "Model B",
  "comparison_rows": [
    { "metric": "Accuracy",  "value_a": "94.2%", "value_b": "91.7%", "winner": "a" },
    { "metric": "Latency",   "value_a": "340ms", "value_b": "180ms", "winner": "b" },
    { "metric": "F1 Score",  "value_a": "0.91",  "value_b": "0.91",  "winner": "tie" }
  ],
  "summary": "Model A is more accurate; Model B is faster...",
  "findings": ["Model B has 47% lower latency"],
  "recommendations": ["Use Model A for batch, Model B for real-time"]
}
```

---

### `output_type: "heatmap"`

A matrix of numeric values — correlation tables, frequency grids, time-of-day activity.

| Field | Type |
|---|---|
| `heatmap_title` | string \| null |
| `heatmap_row_labels` | string[] | Max 10 |
| `heatmap_col_labels` | string[] | Max 10 |
| `heatmap_values` | number[][] | `[row_idx][col_idx]` — same dimensions as labels |

```json
{
  "output_type": "heatmap",
  "heatmap_title": "Error frequency by hour and day",
  "heatmap_row_labels": ["Mon", "Tue", "Wed"],
  "heatmap_col_labels": ["00:00", "06:00", "12:00", "18:00"],
  "heatmap_values": [
    [2, 0, 5, 12],
    [1, 0, 3, 8],
    [4, 1, 6, 15]
  ],
  "summary": "Errors peak at 18:00 across all weekdays...",
  "findings": ["Wednesday 18:00 has the highest error count (15)"],
  "recommendations": ["Schedule maintenance windows at 06:00"]
}
```

---

### `output_type: "report"`

Qualitative or narrative findings. No extra fields — only the common fields apply.

```json
{
  "output_type": "report",
  "summary": "Error rate spiked 300% between 2–4 AM on three consecutive nights.",
  "findings": ["HTTP 500 errors concentrated at 02:15–03:45 UTC"],
  "recommendations": ["Investigate cron job scheduled at 02:00 UTC"],
  "quality_score": 7,
  "quality_verdict": "Question answered but specific error counts were not available."
}
```

---

## Minimal reusable client

### JavaScript (full client)

```js
const API = 'https://pymite6941-data-analyst-ai-agent.hf.space'

/**
 * @param {string} context      - What to analyze
 * @param {File[]} files        - Optional data files (up to 3)
 * @param {(line: string) => void} onLog    - Called for each log line
 * @param {(result: object) => void} onResult - Called with the parsed result object
 */
async function analyze(context, files = [], onLog, onResult) {
  const form = new FormData()
  form.append('context', context)
  files.forEach((f) => form.append('files', f))

  const res = await fetch(`${API}/analyze`, { method: 'POST', body: form })

  if (!res.ok) {
    const { error } = await res.json()
    throw new Error(error)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop()
    for (const raw of lines) {
      if (!raw.startsWith('data: ')) continue
      const payload = JSON.parse(raw.slice(6))
      if (payload === '__DONE__') return
      if (payload?.type === 'result') onResult(JSON.parse(payload.content))
      else onLog(payload)
    }
  }
}

// Usage
analyze(
  'Show me which products had the highest Q3 revenue',
  [document.querySelector('input[type=file]').files[0]],
  (line) => console.log('[LOG]', line),
  (result) => {
    console.log('Type:', result.output_type)
    console.log('Summary:', result.summary)
    console.log('Quality:', result.quality_score, '—', result.quality_verdict)
  },
)
```

### Python (full client)

```python
import json
import requests

API = 'https://pymite6941-data-analyst-ai-agent.hf.space'

def analyze(context, file_paths=None, on_log=print, on_result=print):
    """
    Runs the DataFlow pipeline and streams results.
    
    Args:
        context:    str — what to analyze
        file_paths: list[str] — optional local file paths (up to 3)
        on_log:     callable(str) — called for each log line
        on_result:  callable(dict) — called with the final parsed result
    """
    files = []
    handles = []
    if file_paths:
        for path in file_paths[:3]:
            h = open(path, 'rb')
            handles.append(h)
            files.append(('files', (path.split('/')[-1], h)))

    try:
        res = requests.post(
            f'{API}/analyze',
            data={'context': context},
            files=files or None,
            stream=True,
            timeout=660,   # slightly over the 10-min server timeout
        )
        res.raise_for_status()

        for raw in res.iter_lines():
            line = raw.decode('utf-8')
            if not line.startswith('data: '):
                continue
            payload = json.loads(line[6:])
            if payload == '__DONE__':
                break
            if isinstance(payload, dict) and payload.get('type') == 'result':
                on_result(json.loads(payload['content']))
            else:
                on_log(payload)
    finally:
        for h in handles:
            h.close()


# Usage — no file
analyze('What are the main causes of HTTP 500 errors in an e-commerce backend?')

# Usage — with CSV
analyze(
    context='Show me monthly revenue trends and flag any anomalies',
    file_paths=['sales_2024.csv'],
    on_log=lambda line: print(f'  {line}'),
    on_result=lambda r: print(json.dumps(r, indent=2)),
)
```

---

## Output type reference

| `output_type` | When the agent picks it | Key extra fields |
|---|---|---|
| `code` | Answer is or includes runnable code/queries | `code_blocks` |
| `metrics` | Answer is a set of KPIs or key numbers | `metrics` |
| `comparison` | Two named entities compared across metrics | `comparison_a_label`, `comparison_b_label`, `comparison_rows` |
| `heatmap` | Data is a matrix (correlation, frequency, activity) | `heatmap_row_labels`, `heatmap_col_labels`, `heatmap_values` |
| `table` | Ranked/multi-attribute list | `table_headers`, `table_rows` |
| `chart` | Visual comparison of 2+ numeric values | `chart_type`, `data_points` |
| `report` | Qualitative or narrative findings | *(common fields only)* |

The agent picks the **first** type in this priority order whose condition is met.

---

## Pipeline internals

The `/analyze` endpoint runs a sequential 6-agent CrewAI pipeline:

1. **Context Agent** — rewrites the user's context into an unambiguous analysis directive
2. **Data Quality Inspector** — reads uploaded files, reports column names, row counts, and data issues
3. **Prompt Engineer** — converts the directive + quality report into a step-by-step analysis prompt
4. **Senior Data Analyst** — reads files exactly once with FileReadTool and executes the analysis
5. **Output Formatter** — serialises findings into one of the 7 output types above
6. **QA Critic** — rates the completed analysis 1–10 and writes a verdict

On upstream 429/402/503/529 errors, the pipeline automatically rotates across a pool of Groq and OpenRouter free-tier models and retries. Hard timeout: 10 minutes.
