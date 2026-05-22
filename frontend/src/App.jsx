import { useState, useRef, useEffect, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer
} from 'recharts'
import './App.css'

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'
const CHART_COLORS = ['#6366f1', '#34d399', '#f472b6', '#fbbf24', '#60a5fa', '#a78bfa', '#fb923c']
const HISTORY_KEY = 'dataflow_history'
const MAX_HISTORY = 5

const PIPELINE_NODES = [
  { id: 'context',   label: 'Context',    match: 'analysis directive specialist', icon: '◈', color: '#6366f1' },
  { id: 'prompt',    label: 'Prompt Eng', match: 'data analysis prompt engineer', icon: '✦', color: '#8b5cf6' },
  { id: 'analyst',   label: 'Analyst',    match: 'senior data analyst',           icon: '⬡', color: '#34d399' },
  { id: 'formatter', label: 'Formatter',  match: 'structured output specialist',  icon: '◉', color: '#f472b6' },
]

// ── Helpers ───────────────────────────────────────────────────────────────────

function detectAgentIndex(line) {
  const l = line.toLowerCase()
  for (let i = 0; i < PIPELINE_NODES.length; i++) {
    if (l.includes(PIPELINE_NODES[i].match)) return i
  }
  return -1
}

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]') } catch { return [] }
}

function saveHistory(entries) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(entries)) } catch {}
}

function timeAgo(ts) {
  const diff = Date.now() - new Date(ts).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function getLogClass(line) {
  const l = line.toLowerCase()
  if (l.startsWith('[error]') || l.includes('traceback') || l.includes('exception')) return 'log-error'
  if (l.includes('warning') || l.includes('warn')) return 'log-warn'
  if (detectAgentIndex(line) !== -1 && (l.includes('agent:') || l.includes('# agent') || l.includes('working agent'))) return 'log-agent-header'
  if (l.includes('agent:') || l.includes('task:') || l.includes('> entering') || l.includes('crew')) return 'log-agent'
  if (l.includes('final answer') || l.includes('completed') || l.includes('finished')) return 'log-success'
  if (l.includes('thought:') || l.includes('action:') || l.includes('observation:')) return 'log-step'
  return ''
}

function parseResult(content) {
  try {
    const parsed = JSON.parse(content.trim())
    if (parsed.output_type === 'chart' || parsed.output_type === 'report') return { type: 'structured', data: parsed }
    return { type: 'json', data: parsed }
  } catch {}
  const lines = content.trim().split('\n').filter(l => l.trim())
  if (lines.length >= 2) {
    const cols = lines[0].split(',').length
    if (cols > 1 && lines.slice(1, 5).every(l => l.split(',').length === cols)) return { type: 'csv', data: content }
  }
  return { type: 'text', data: content }
}

function triggerDownload(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

// ── Typewriter hook ───────────────────────────────────────────────────────────

function useTypewriter(text, speed = 10) {
  const [displayed, setDisplayed] = useState('')
  const [done, setDone] = useState(false)
  const prevRef = useRef('')

  useEffect(() => {
    if (!text) { setDisplayed(''); setDone(false); return }
    if (text === prevRef.current) return
    prevRef.current = text
    setDisplayed('')
    setDone(false)
    let i = 0
    const id = setInterval(() => {
      i++
      setDisplayed(text.slice(0, i))
      if (i >= text.length) { setDone(true); clearInterval(id) }
    }, speed)
    return () => clearInterval(id)
  }, [text, speed])

  return { displayed, done }
}

// ── Pipeline diagram ──────────────────────────────────────────────────────────

function PipelineDiagram({ logs, status }) {
  const seen = useMemo(() => {
    const s = new Set()
    for (const line of logs) {
      const idx = detectAgentIndex(line)
      if (idx !== -1) s.add(idx)
    }
    return s
  }, [logs])

  const activeIdx = useMemo(() => {
    if (status !== 'running') return -1
    for (let i = logs.length - 1; i >= 0; i--) {
      const idx = detectAgentIndex(logs[i])
      if (idx !== -1) return idx
    }
    return status === 'running' ? 0 : -1
  }, [logs, status])

  const items = []
  PIPELINE_NODES.forEach((node, i) => {
    const isActive = activeIdx === i
    const isDone = seen.has(i) && (status === 'done' || (activeIdx !== -1 && activeIdx > i))
    const isLit = isActive || isDone
    items.push(
      <div
        key={node.id}
        className={`pipeline-node${isActive ? ' pipeline-node--active' : ''}${isDone ? ' pipeline-node--done' : ''}${!isLit ? ' pipeline-node--idle' : ''}`}
        style={{ '--nc': node.color }}
      >
        <span className="pipeline-node-icon">{isDone ? '✓' : node.icon}</span>
        <span className="pipeline-node-label">{node.label}</span>
      </div>
    )
    if (i < PIPELINE_NODES.length - 1) {
      items.push(
        <div key={`conn-${i}`} className={`pipeline-conn${isLit ? ' pipeline-conn--lit' : ''}`}>
          <div className="pipeline-conn-line" />
          <div className="pipeline-conn-arrow">›</div>
        </div>
      )
    }
  })

  return <div className="pipeline">{items}</div>
}

// ── Download popup ────────────────────────────────────────────────────────────

function DownloadPopup({ parsed, onClose }) {
  const options = []
  if (parsed.type === 'structured') {
    const d = parsed.data
    const reportMd = [
      `# ${d.chart_title || 'Analysis Report'}`,
      `\n## Summary\n${d.summary}`,
      `\n## Findings\n${d.findings.map(f => `- ${f}`).join('\n')}`,
      `\n## Recommendations\n${d.recommendations.map(r => `- ${r}`).join('\n')}`,
    ].join('\n')
    options.push({ icon: '{}', label: 'Full JSON', ext: 'json', content: JSON.stringify(d, null, 2) })
    options.push({ icon: '📝', label: 'Report (Markdown)', ext: 'md', content: reportMd })
    if (d.data_points?.length) {
      const csv = ['label,value,category', ...d.data_points.map(p => `${p.label},${p.value},${p.category ?? ''}`)]
      options.push({ icon: '⊞', label: 'Chart Data (CSV)', ext: 'csv', content: csv.join('\n') })
    }
  } else if (parsed.type === 'json') {
    options.push({ icon: '{}', label: 'JSON', ext: 'json', content: JSON.stringify(parsed.data, null, 2) })
  } else if (parsed.type === 'csv') {
    options.push({ icon: '⊞', label: 'CSV', ext: 'csv', content: parsed.data })
  }
  options.push({ icon: '📄', label: 'Plain Text', ext: 'txt', content: typeof parsed.data === 'string' ? parsed.data : JSON.stringify(parsed.data, null, 2) })

  return (
    <div className="popup-overlay" onClick={onClose}>
      <div className="popup" onClick={e => e.stopPropagation()}>
        <div className="popup-header">
          <span>Download Result</span>
          <button className="popup-close" onClick={onClose}>×</button>
        </div>
        <div className="popup-body">
          {options.map((opt, i) => (
            <button key={i} className="popup-option" onClick={() => { triggerDownload(`result.${opt.ext}`, opt.content); onClose() }}>
              <span className="popup-option-icon">{opt.icon}</span>
              <div className="popup-option-info">
                <span className="popup-option-label">{opt.label}</span>
                <span className="popup-option-ext">.{opt.ext}</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── Chart renderer ────────────────────────────────────────────────────────────

function ChartView({ data }) {
  const points = data.data_points ?? []
  const chartData = points.map(p => ({ name: p.label, value: p.value, category: p.category }))
  const tooltipStyle = { background: '#13141a', border: '1px solid #1e2030', borderRadius: 8, fontSize: 12 }

  if (data.chart_type === 'pie') {
    return (
      <ResponsiveContainer width="100%" height={300}>
        <PieChart>
          <Pie data={chartData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={110} label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={false}>
            {chartData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
          </Pie>
          <Tooltip contentStyle={tooltipStyle} />
          <Legend wrapperStyle={{ fontSize: 12, color: '#6b7280' }} />
        </PieChart>
      </ResponsiveContainer>
    )
  }

  if (data.chart_type === 'line') {
    return (
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e2030" />
          <XAxis dataKey="name" tick={{ fill: '#4a4f6a', fontSize: 11 }} />
          <YAxis tick={{ fill: '#4a4f6a', fontSize: 11 }} label={{ value: data.y_axis_label, angle: -90, position: 'insideLeft', fill: '#3d405a', fontSize: 11 }} />
          <Tooltip contentStyle={tooltipStyle} />
          <Line type="monotone" dataKey="value" stroke="#6366f1" strokeWidth={2} dot={{ fill: '#6366f1', r: 4 }} activeDot={{ r: 6 }} />
        </LineChart>
      </ResponsiveContainer>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={chartData} margin={{ top: 5, right: 20, bottom: 30, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2030" vertical={false} />
        <XAxis dataKey="name" tick={{ fill: '#4a4f6a', fontSize: 11 }} angle={-30} textAnchor="end" interval={0} />
        <YAxis tick={{ fill: '#4a4f6a', fontSize: 11 }} label={{ value: data.y_axis_label, angle: -90, position: 'insideLeft', fill: '#3d405a', fontSize: 11 }} />
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: 'rgba(99,102,241,0.06)' }} />
        <Bar dataKey="value" radius={[4, 4, 0, 0]}>
          {chartData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Result views ──────────────────────────────────────────────────────────────

function StructuredReport({ data }) {
  const { displayed: sumDisplayed, done: sumDone } = useTypewriter(data.summary, 10)

  return (
    <div className="structured-report">
      <div className="report-summary">
        {sumDisplayed}
        {!sumDone && <span className="typewriter-cursor" />}
      </div>
      <div className={`report-sections${sumDone ? ' report-sections--visible' : ''}`}>
        {data.findings?.length > 0 && (
          <div className="report-section">
            <h3 className="report-section-title">Findings</h3>
            <ul className="report-list">
              {data.findings.map((f, i) => <li key={i}><ReactMarkdown remarkPlugins={[remarkGfm]}>{f}</ReactMarkdown></li>)}
            </ul>
          </div>
        )}
        {data.recommendations?.length > 0 && (
          <div className="report-section">
            <h3 className="report-section-title">Recommendations</h3>
            <ul className="report-list report-list--rec">
              {data.recommendations.map((r, i) => <li key={i}><ReactMarkdown remarkPlugins={[remarkGfm]}>{r}</ReactMarkdown></li>)}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

function ResultView({ parsed }) {
  if (parsed.type === 'structured') return <StructuredReport data={parsed.data} />
  if (parsed.type === 'json') return <pre className="result-code result-json">{JSON.stringify(parsed.data, null, 2)}</pre>
  if (parsed.type === 'csv') {
    const lines = parsed.data.trim().split('\n').filter(l => l.trim())
    const headers = lines[0].split(',').map(h => h.trim().replace(/^"|"$/g, ''))
    const rows = lines.slice(1).map(l => l.split(',').map(c => c.trim().replace(/^"|"$/g, '')))
    return (
      <div className="result-table-wrap">
        <table className="result-table">
          <thead><tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
          <tbody>{rows.map((row, i) => <tr key={i}>{row.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
        </table>
      </div>
    )
  }
  return (
    <div className="result-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{parsed.data}</ReactMarkdown>
    </div>
  )
}

// ── History drawer ────────────────────────────────────────────────────────────

const TYPE_COLORS = { chart: '#a78bfa', report: '#34d399', json: '#60a5fa', csv: '#fbbf24', text: '#94a3b8' }

function HistoryDrawer({ history, onSelect, onClose }) {
  return (
    <div className="popup-overlay" onClick={onClose}>
      <div className="history-drawer" onClick={e => e.stopPropagation()}>
        <div className="popup-header">
          <span>Session History</span>
          <button className="popup-close" onClick={onClose}>×</button>
        </div>
        <div className="history-list">
          {history.length === 0 ? (
            <div className="history-empty">No saved sessions yet. Complete an analysis to save it.</div>
          ) : history.map(entry => {
            const c = TYPE_COLORS[entry.resultType] || '#94a3b8'
            return (
              <button key={entry.id} className="history-item" onClick={() => onSelect(entry)}>
                <div className="history-item-top">
                  <span className="history-item-badge" style={{ color: c, borderColor: c + '44', background: c + '11' }}>{(entry.resultType || 'unknown').toUpperCase()}</span>
                  <span className="history-item-time">{timeAgo(entry.timestamp)}</span>
                </div>
                <p className="history-item-context">{entry.context.slice(0, 90)}{entry.context.length > 90 ? '…' : ''}</p>
                <div className="history-item-meta">{entry.logCount} log lines{entry.fileName ? ` · ${entry.fileName}` : ''}</div>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ── Main app ──────────────────────────────────────────────────────────────────

export default function App() {
  const [context, setContext] = useState('')
  const [file, setFile] = useState(null)
  const [logs, setLogs] = useState([])
  const [result, setResult] = useState(null)
  const [status, setStatus] = useState('idle')
  const [showLog, setShowLog] = useState(true)
  const [activeTab, setActiveTab] = useState('logs')
  const [showDownload, setShowDownload] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState(() => loadHistory())
  const logEndRef = useRef(null)
  const fileInputRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => {
    if (activeTab === 'logs') logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs, activeTab])

  useEffect(() => {
    if (result) setActiveTab('result')
  }, [result])

  // Save completed run to history
  useEffect(() => {
    if (status !== 'done' || !result) return
    const p = parseResult(result)
    const entry = {
      id: Date.now(),
      timestamp: new Date().toISOString(),
      context,
      fileName: file?.name ?? null,
      result,
      logCount: logs.length,
      resultType: p.type === 'structured' ? p.data.output_type : p.type,
    }
    const updated = [entry, ...history].slice(0, MAX_HISTORY)
    setHistory(updated)
    saveHistory(updated)
  }, [status]) // eslint-disable-line

  const parsed = result ? parseResult(result) : null
  const hasChart = parsed?.type === 'structured' && parsed.data?.output_type === 'chart' && parsed.data?.data_points?.length > 0

  async function handleAnalyze() {
    if (!context.trim() || status === 'running') return
    setLogs([]); setResult(null); setActiveTab('logs'); setStatus('running'); setShowLog(true)

    const form = new FormData()
    form.append('context', context)
    if (file) form.append('file', file)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const res = await fetch(`${API}/analyze`, { method: 'POST', body: form, signal: controller.signal })
      if (!res.ok) { setLogs([`[ERROR] Server returned ${res.status}`]); setStatus('error'); return }

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
          let payload
          try { payload = JSON.parse(raw.slice(6)) } catch { continue }
          if (payload === '__DONE__') { setStatus('done'); return }
          if (payload?.type === 'result') { setResult(payload.content) }
          else { setLogs(prev => [...prev, payload]) }
        }
      }
      setStatus('done')
    } catch (err) {
      if (err.name !== 'AbortError') { setLogs(prev => [...prev, `[ERROR] ${err.message}`]); setStatus('error') }
    }
  }

  function handleReset() { setLogs([]); setResult(null); setStatus('idle'); setActiveTab('logs'); setFile(null); setContext('') }

  function handleHistorySelect(entry) {
    setContext(entry.context)
    setResult(entry.result)
    setLogs([])
    setStatus('done')
    setActiveTab('result')
    setFile(null)
    setShowHistory(false)
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-brand">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <polygon points="10,1 18,5.5 18,14.5 10,19 2,14.5 2,5.5" stroke="currentColor" strokeWidth="1.5" fill="none"/>
            <circle cx="10" cy="10" r="2.5" fill="currentColor"/>
          </svg>
          <span>DataFlow</span>
        </div>
        <div className="topbar-right">
          <span className="topbar-tag">AI Agent Pipeline</span>
          <button className="toggle-log-btn" onClick={() => setShowHistory(true)}>
            History
            {history.length > 0 && <span className="toggle-log-count">{history.length}</span>}
          </button>
          <button className={`toggle-log-btn${showLog ? ' toggle-log-btn--active' : ''}`} onClick={() => setShowLog(v => !v)}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" strokeWidth="1.3"/><line x1="6" y1="2" x2="6" y2="14" stroke="currentColor" strokeWidth="1.3"/></svg>
            {showLog ? 'Hide Output' : 'Show Output'}
            {!showLog && logs.length > 0 && <span className="toggle-log-count">{logs.length}</span>}
          </button>
        </div>
      </header>

      <main className={`workspace${showLog ? '' : ' workspace--log-hidden'}`}>
        <section className="input-panel">
          <h1 className="panel-title">What do you want to analyze?</h1>
          <p className="panel-sub">Describe your dataset and the question you want answered. The agent pipeline will interpret your request, clean the data, and extract insights.</p>

          <div className="field">
            <label className="field-label">Context</label>
            <textarea className="context-area" placeholder="e.g. I have a CSV of monthly sales. Show me the top 5 products by revenue in Q3 and flag any anomalies…" value={context} onChange={e => setContext(e.target.value)} rows={5} disabled={status === 'running'} />
          </div>

          <div className="field">
            <label className="field-label">Data file <span className="field-opt">optional</span></label>
            <div className={`dropzone${file ? ' dropzone--has-file' : ''}`} onDrop={e => { e.preventDefault(); setFile(e.dataTransfer.files[0] || null) }} onDragOver={e => e.preventDefault()} onClick={() => fileInputRef.current?.click()} role="button" tabIndex={0} onKeyDown={e => e.key === 'Enter' && fileInputRef.current?.click()}>
              <input ref={fileInputRef} type="file" accept=".csv,.json,.txt,.pdf,.xml" onChange={e => setFile(e.target.files[0] || null)} style={{ display: 'none' }} />
              {file ? (
                <span className="dropzone-filename"><span className="dropzone-icon">📄</span> {file.name}
                  <button className="dropzone-clear" onClick={e => { e.stopPropagation(); setFile(null) }}>×</button>
                </span>
              ) : (
                <span className="dropzone-hint">Drop a file here or <u>click to upload</u><span className="dropzone-types">CSV · JSON · TXT · PDF · XML</span></span>
              )}
            </div>
          </div>

          <div className="action-row">
            <button className="btn btn-primary" onClick={handleAnalyze} disabled={!context.trim() || status === 'running'}>
              {status === 'running' ? <><span className="btn-spinner" /> Running…</> : 'Run Analysis'}
            </button>
            {status === 'running' && <button className="btn btn-ghost" onClick={() => { abortRef.current?.abort(); setStatus('idle') }}>Stop</button>}
            {(status === 'done' || status === 'error') && <button className="btn btn-ghost" onClick={handleReset}>Reset</button>}
          </div>
        </section>

        <section className="log-panel">
          <PipelineDiagram logs={logs} status={status} />

          <div className="tab-bar">
            <div className="tab-bar-left">
              <div className="log-dots">
                <div className="log-dot log-dot-r" /><div className="log-dot log-dot-y" /><div className="log-dot log-dot-g" />
              </div>
              <button className={`tab-btn${activeTab === 'logs' ? ' tab-btn--active' : ''}`} onClick={() => setActiveTab('logs')}>
                Logs {logs.length > 0 && <span className="tab-count">{logs.length}</span>}
              </button>
              <button className={`tab-btn${activeTab === 'result' ? ' tab-btn--active' : ''}${parsed ? ' tab-btn--has-result' : ''}`} onClick={() => setActiveTab('result')} disabled={!parsed}>
                Result {parsed && <span className="tab-count tab-count--result">✓</span>}
              </button>
              {hasChart && (
                <button className={`tab-btn${activeTab === 'artifacts' ? ' tab-btn--active' : ''} tab-btn--chart`} onClick={() => setActiveTab('artifacts')}>
                  Artifacts <span className="tab-count tab-count--chart">📊</span>
                </button>
              )}
            </div>
            {status !== 'idle' && (
              <span className={`log-badge log-badge--${status}`}>
                {status === 'running' && <span className="badge-pulse" />}
                {status === 'running' ? 'Live' : status === 'done' ? 'Complete' : 'Error'}
              </span>
            )}
          </div>

          {activeTab === 'logs' && <>
            <div className="log-terminal" role="log" aria-live="polite">
              {logs.length === 0 && status === 'idle' && <div className="log-empty"><span className="log-empty-icon">⬡</span><span>Agent output will stream here in real-time.</span></div>}
              {logs.length === 0 && status === 'running' && <div className="log-empty log-empty--active"><span>Initializing agent pipeline</span></div>}
              {logs.map((line, i) => {
                const cls = getLogClass(line)
                if (cls === 'log-agent-header') {
                  const agentIdx = detectAgentIndex(line)
                  if (agentIdx !== -1) {
                    const node = PIPELINE_NODES[agentIdx]
                    return (
                      <div key={i} className="log-step-card" style={{ '--nc': node.color }}>
                        <span className="log-step-card-icon">{node.icon}</span>
                        <span className="log-step-card-name">{node.label}</span>
                      </div>
                    )
                  }
                }
                return (
                  <div key={i} className={`log-line ${cls}`}>
                    <span className="log-gutter">{String(i + 1).padStart(3, ' ')}</span>
                    <span className="log-text">{line}</span>
                  </div>
                )
              })}
              <div ref={logEndRef} />
            </div>
            {logs.length > 0 && <div className="log-footer"><span>{logs.length} lines</span><button className="log-copy" onClick={() => navigator.clipboard.writeText(logs.join('\n'))}>Copy all</button></div>}
          </>}

          {activeTab === 'result' && (
            <div className="result-panel">
              {!parsed ? (
                <div className="log-empty"><span className="log-empty-icon">⬡</span><span>Result will appear once analysis completes.</span></div>
              ) : <>
                <div className="result-type-bar">
                  <span className="result-type-badge">{parsed.type === 'structured' ? parsed.data.output_type.toUpperCase() : parsed.type.toUpperCase()}</span>
                  {parsed.type === 'structured' && parsed.data.chart_type && <span className="result-chart-badge">{parsed.data.chart_type} chart</span>}
                </div>
                <div className="result-body"><ResultView parsed={parsed} /></div>
                <div className="log-footer">
                  <span>{result.length} chars</span>
                  <button className="btn-download" onClick={() => setShowDownload(true)}>⬇ Download</button>
                </div>
              </>}
            </div>
          )}

          {activeTab === 'artifacts' && hasChart && (
            <div className="result-panel">
              <div className="result-type-bar">
                <span className="result-type-badge">CHART</span>
                <span className="result-chart-badge">{parsed.data.chart_type}</span>
              </div>
              <div className="artifact-body">
                <h2 className="chart-title">{parsed.data.chart_title}</h2>
                {parsed.data.x_axis_label && <p className="chart-axis-label">X: {parsed.data.x_axis_label} · Y: {parsed.data.y_axis_label}</p>}
                <ChartView data={parsed.data} />
                <div className="artifact-stats">
                  {parsed.data.data_points.map((p, i) => (
                    <div key={i} className="stat-chip" style={{ borderColor: CHART_COLORS[i % CHART_COLORS.length] + '44', background: CHART_COLORS[i % CHART_COLORS.length] + '11' }}>
                      <span className="stat-label">{p.label}</span>
                      <span className="stat-value" style={{ color: CHART_COLORS[i % CHART_COLORS.length] }}>{p.value.toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="log-footer">
                <span>{parsed.data.data_points.length} data points</span>
                <button className="btn-download" onClick={() => setShowDownload(true)}>⬇ Download</button>
              </div>
            </div>
          )}
        </section>
      </main>

      {showDownload && parsed && <DownloadPopup parsed={parsed} onClose={() => setShowDownload(false)} />}
      {showHistory && <HistoryDrawer history={history} onSelect={handleHistorySelect} onClose={() => setShowHistory(false)} />}
    </div>
  )
}
