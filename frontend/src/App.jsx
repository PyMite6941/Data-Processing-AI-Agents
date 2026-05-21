import { useState, useRef, useEffect } from 'react'
import './App.css'

const API = 'http://localhost:8000'

function getLogClass(line) {
  const l = line.toLowerCase()
  if (l.startsWith('[error]') || l.includes('traceback') || l.includes('exception')) return 'log-error'
  if (l.includes('warning') || l.includes('warn')) return 'log-warn'
  if (l.includes('agent:') || l.includes('task:') || l.includes('> entering') || l.includes('crew')) return 'log-agent'
  if (l.includes('final answer') || l.includes('completed') || l.includes('finished') || l.includes('> finished')) return 'log-success'
  if (l.includes('thought:') || l.includes('action:') || l.includes('observation:')) return 'log-step'
  return ''
}

export default function App() {
  const [context, setContext] = useState('')
  const [file, setFile] = useState(null)
  const [logs, setLogs] = useState([])
  const [status, setStatus] = useState('idle')
  const [showLog, setShowLog] = useState(true)
  const logEndRef = useRef(null)
  const fileInputRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  async function handleAnalyze() {
    if (!context.trim() || status === 'running') return
    setLogs([])
    setStatus('running')
    setShowLog(true)

    const form = new FormData()
    form.append('context', context)
    if (file) form.append('file', file)

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const res = await fetch(`${API}/analyze`, {
        method: 'POST',
        body: form,
        signal: controller.signal,
      })

      if (!res.ok) {
        setLogs([`[ERROR] Server returned ${res.status}`])
        setStatus('error')
        return
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
          let payload
          try { payload = JSON.parse(raw.slice(6)) } catch { continue }
          if (payload === '__DONE__') { setStatus('done'); return }
          setLogs(prev => [...prev, payload])
        }
      }
      setStatus('done')
    } catch (err) {
      if (err.name !== 'AbortError') {
        setLogs(prev => [...prev, `[ERROR] ${err.message}`])
        setStatus('error')
      }
    }
  }

  function handleStop() {
    abortRef.current?.abort()
    setStatus('idle')
  }

  function handleDrop(e) {
    e.preventDefault()
    const dropped = e.dataTransfer.files[0]
    if (dropped) setFile(dropped)
  }

  function handleReset() {
    setLogs([])
    setStatus('idle')
    setFile(null)
    setContext('')
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
          <button
            className={`toggle-log-btn${showLog ? ' toggle-log-btn--active' : ''}`}
            onClick={() => setShowLog(v => !v)}
            title={showLog ? 'Hide agent output' : 'Show agent output'}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" strokeWidth="1.3"/>
              <line x1="6" y1="2" x2="6" y2="14" stroke="currentColor" strokeWidth="1.3"/>
            </svg>
            {showLog ? 'Hide Output' : 'Show Output'}
            {!showLog && logs.length > 0 && (
              <span className="toggle-log-count">{logs.length}</span>
            )}
          </button>
        </div>
      </header>

      <main className={`workspace${showLog ? '' : ' workspace--log-hidden'}`}>
        <section className="input-panel">
          <h1 className="panel-title">What do you want to analyze?</h1>
          <p className="panel-sub">
            Describe your dataset and the question you want answered. The agent pipeline
            will interpret your request, clean the data, and extract insights.
          </p>

          <div className="field">
            <label className="field-label">Context</label>
            <textarea
              className="context-area"
              placeholder="e.g. I have a CSV of monthly sales. Show me the top 5 products by revenue in Q3 and flag any anomalies in the trend…"
              value={context}
              onChange={e => setContext(e.target.value)}
              rows={5}
              disabled={status === 'running'}
            />
          </div>

          <div className="field">
            <label className="field-label">
              Data file <span className="field-opt">optional</span>
            </label>
            <div
              className={`dropzone${file ? ' dropzone--has-file' : ''}`}
              onDrop={handleDrop}
              onDragOver={e => e.preventDefault()}
              onClick={() => fileInputRef.current?.click()}
              role="button"
              tabIndex={0}
              onKeyDown={e => e.key === 'Enter' && fileInputRef.current?.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv,.json,.txt,.pdf,.xml"
                onChange={e => setFile(e.target.files[0] || null)}
                style={{ display: 'none' }}
              />
              {file ? (
                <span className="dropzone-filename">
                  <span className="dropzone-icon">📄</span> {file.name}
                  <button
                    className="dropzone-clear"
                    onClick={e => { e.stopPropagation(); setFile(null) }}
                    aria-label="Remove file"
                  >×</button>
                </span>
              ) : (
                <span className="dropzone-hint">
                  Drop a file here or <u>click to upload</u>
                  <span className="dropzone-types">CSV · JSON · TXT · PDF · XML</span>
                </span>
              )}
            </div>
          </div>

          <div className="action-row">
            <button
              className="btn btn-primary"
              onClick={handleAnalyze}
              disabled={!context.trim() || status === 'running'}
            >
              {status === 'running'
                ? <><span className="btn-spinner" /> Running…</>
                : 'Run Analysis'}
            </button>
            {status === 'running' && (
              <button className="btn btn-ghost" onClick={handleStop}>Stop</button>
            )}
            {(status === 'done' || status === 'error') && (
              <button className="btn btn-ghost" onClick={handleReset}>Reset</button>
            )}
          </div>
        </section>

        <section className="log-panel">
          <div className="log-header">
            <div className="log-header-left">
              <div className="log-dots">
                <div className="log-dot log-dot-r" />
                <div className="log-dot log-dot-y" />
                <div className="log-dot log-dot-g" />
              </div>
              <span className="log-title">Agent Output</span>
            </div>
            {status !== 'idle' && (
              <span className={`log-badge log-badge--${status}`}>
                {status === 'running' && <span className="badge-pulse" />}
                {status === 'running' ? 'Live' : status === 'done' ? 'Complete' : 'Error'}
              </span>
            )}
          </div>

          <div className="log-terminal" role="log" aria-live="polite">
            {logs.length === 0 && status === 'idle' && (
              <div className="log-empty">
                <span className="log-empty-icon">⬡</span>
                <span>Agent output will stream here in real-time.</span>
              </div>
            )}
            {logs.length === 0 && status === 'running' && (
              <div className="log-empty log-empty--active">
                <span>Initializing agent pipeline</span>
              </div>
            )}
            {logs.map((line, i) => (
              <div key={i} className={`log-line ${getLogClass(line)}`}>
                <span className="log-gutter">{String(i + 1).padStart(3, ' ')}</span>
                <span className="log-text">{line}</span>
              </div>
            ))}
            <div ref={logEndRef} />
          </div>

          {logs.length > 0 && (
            <div className="log-footer">
              <span>{logs.length} line{logs.length !== 1 ? 's' : ''}</span>
              <button
                className="log-copy"
                onClick={() => navigator.clipboard.writeText(logs.join('\n'))}
              >Copy all</button>
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
