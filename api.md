# DataFlow API Reference

## Endpoint

```
POST {API_URL}/analyze
```

The base URL is set via the `VITE_API_URL` environment variable in the frontend (falls back to `http://localhost:8000`). The HuggingFace Space runs on port 7860.

---

## Request

**Content-Type:** `multipart/form-data`

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `context` | string | Yes | Max 2000 chars | Natural-language description of what to analyze |
| `file` | File (binary) | No | Max 10MB; `.csv`, `.json`, `.txt`, `.pdf`, `.xml` | Data file to analyze |

### Example (JavaScript)

```js
const form = new FormData()
form.append('context', 'Show me the top 5 products by revenue in Q3')
form.append('file', fileInputElement.files[0])   // optional

const res = await fetch(`${API_URL}/analyze`, { method: 'POST', body: form })
```

### Example (Python / requests)

```python
import requests

res = requests.post(
    'https://your-hf-space.hf.space/analyze',
    data={'context': 'Show me trends in error rates over the last 30 days'},
    files={'file': open('server.log', 'rb')},   # optional
    stream=True
)
```

### Error responses (before stream opens)

If input validation fails, a normal JSON response is returned immediately:

```json
{ "error": "Context is required." }
{ "error": "Context too long (2100 chars, max 2000)." }
{ "error": "File type '.exe' not supported. Allowed: .csv, .json, .pdf, .txt, .xml" }
{ "error": "File too large (12288KB, max 10MB)." }
```

---

## Response — Server-Sent Events (SSE)

The response is a **streaming SSE connection** that stays open while the 4-agent pipeline runs. Each frame is:

```
data: <JSON-encoded payload>\n\n
```

Three payload shapes arrive in this order:

| Shape | When | What to do |
|---|---|---|
| `"some log string"` | Throughout the run | Display in a live log feed |
| `{"type": "result", "content": "..."}` | When pipeline completes | Parse and render the result |
| `"__DONE__"` | After result (or on timeout) | Close the stream |

A heartbeat comment (`: ping`) is sent every 30s on idle to keep the connection alive through proxies.

### Reading loop (JavaScript)

```js
const reader = res.body.getReader()
const decoder = new TextDecoder()
let buf = ''

while (true) {
  const { done, value } = await reader.read()
  if (done) break

  buf += decoder.decode(value, { stream: true })
  const lines = buf.split('\n')
  buf = lines.pop()                          // hold incomplete line

  for (const raw of lines) {
    if (!raw.startsWith('data: ')) continue
    const payload = JSON.parse(raw.slice(6)) // strip "data: " prefix

    if (payload === '__DONE__') return
    if (payload?.type === 'result') {
      handleResult(payload.content)          // final structured output
    } else {
      appendLog(payload)                     // payload is a string log line
    }
  }
}
```

### Reading loop (Python)

```python
for raw in res.iter_lines():
    raw = raw.decode()
    if not raw.startswith('data: '):
        continue
    payload = json.loads(raw[6:])
    if payload == '__DONE__':
        break
    if isinstance(payload, dict) and payload.get('type') == 'result':
        handle_result(payload['content'])
    else:
        print(payload)   # log line string
```

---

## Result payload schema

`payload.content` is a JSON string. Parse it with `JSON.parse` / `json.loads`.

```ts
{
  output_type: "chart" | "report",

  // Chart fields (present when output_type === "chart")
  chart_type:    "bar" | "line" | "pie",
  chart_title:   string,
  x_axis_label:  string,
  y_axis_label:  string,
  data_points: [
    { label: string, value: number, category?: string }
  ],

  // Report fields (always present)
  summary:         string,
  findings:        string[],
  recommendations: string[]
}
```

### Example result (chart)

```json
{
  "output_type": "chart",
  "chart_type": "bar",
  "chart_title": "Top 5 Products by Revenue — Q3",
  "x_axis_label": "Product",
  "y_axis_label": "Revenue (USD)",
  "data_points": [
    { "label": "Widget A", "value": 84200, "category": "hardware" },
    { "label": "Widget B", "value": 61500, "category": "hardware" }
  ],
  "summary": "Widget A led Q3 with $84,200 in revenue...",
  "findings": ["Widget A outperformed Q2 by 18%"],
  "recommendations": ["Increase Widget A inventory for Q4"]
}
```

### Example result (report)

```json
{
  "output_type": "report",
  "summary": "Error rate spiked 300% between 2–4 AM on three consecutive nights.",
  "findings": ["HTTP 500 errors concentrated at 02:15–03:45 UTC"],
  "recommendations": ["Investigate cron job scheduled at 02:00 UTC"]
}
```

---

## Minimal reusable client

```js
const API_URL = 'https://pymite6941-data-analyst-ai-agent.hf.space'

async function analyze(contextText, file, onLog, onResult) {
  const form = new FormData()
  form.append('context', contextText)
  if (file) form.append('file', file)

  const res = await fetch(`${API_URL}/analyze`, { method: 'POST', body: form })

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
    const lines = buf.split('\n'); buf = lines.pop()
    for (const raw of lines) {
      if (!raw.startsWith('data: ')) continue
      const payload = JSON.parse(raw.slice(6))
      if (payload === '__DONE__') return
      if (payload?.type === 'result') onResult(JSON.parse(payload.content))
      else onLog(payload)
    }
  }
}
```

---

## Health check

```
GET {API_URL}/health
→ { "status": "ok" }
```

---

## Backend pipeline (internal)

The `/analyze` endpoint runs a 4-agent CrewAI pipeline sequentially:

1. **Context Agent** — rewrites the user's context into a structured analysis directive
2. **Prompt Engineer** — produces a precise step-by-step analysis prompt
3. **Data Analyst** — reads the file (using the appropriate search tool) and executes the analysis
4. **Output Formatter** — structures the result as the JSON schema above

On upstream 429/402/503/529 errors, the pipeline automatically rotates to the next model in its pool and retries. On 404 (model not found), it rotates immediately. The pipeline has a 10-minute hard timeout.
