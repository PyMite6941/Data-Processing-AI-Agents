import { useState, useRef, useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
	BarChart, Bar,
	LineChart, Line,
	PieChart, Pie,
	ScatterChart, Scatter, ZAxis,
	FunnelChart, Funnel, LabelList,
	RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
	Cell, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';
import './App.css';

const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';
const CHART_COLORS = [
	'#6366f1',
	'#34d399',
	'#f472b6',
	'#fbbf24',
	'#60a5fa',
	'#a78bfa',
	'#fb923c',
];
const HISTORY_KEY = 'dataflow_history';
const MAX_HISTORY = 5;

const PIPELINE_NODES = [
	{ id: 'context', label: 'Context', match: 'analysis directive specialist', icon: '◈', color: '#6366f1' },
	{ id: 'cleaner', label: 'Cleaner', match: 'data quality inspector', icon: '✧', color: '#60a5fa' },
	{ id: 'prompt', label: 'Prompt Eng', match: 'data analysis prompt engineer', icon: '✦', color: '#8b5cf6' },
	{ id: 'analyst', label: 'Analyst', match: 'senior data analyst', icon: '⬡', color: '#34d399' },
	{ id: 'formatter', label: 'Formatter', match: 'structured output specialist', icon: '◉', color: '#f472b6' },
	{ id: 'qa', label: 'QA Critic', match: 'analysis quality critic', icon: '◎', color: '#fb923c' },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

function detectAgentIndex(line) {
	const l = line.toLowerCase();
	for (let i = 0; i < PIPELINE_NODES.length; i++) {
		if (l.includes(PIPELINE_NODES[i].match)) return i;
	}
	return -1;
}

function loadHistory() {
	try {
		return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
	} catch {
		return [];
	}
}

function saveHistory(entries) {
	try {
		localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
	} catch {}
}

function timeAgo(ts) {
	const diff = Date.now() - new Date(ts).getTime();
	const m = Math.floor(diff / 60000);
	if (m < 1) return 'just now';
	if (m < 60) return `${m}m ago`;
	const h = Math.floor(m / 60);
	if (h < 24) return `${h}h ago`;
	return `${Math.floor(h / 24)}d ago`;
}

function getLogClass(line) {
	const l = line.toLowerCase();
	if (l.startsWith('[rotate]') || l.startsWith('[retry]')) return 'log-rotate';
	if (
		l.startsWith('[error]') ||
		l.includes('traceback') ||
		l.includes('exception')
	)
		return 'log-error';
	if (l.includes('warning') || l.includes('warn')) return 'log-warn';
	if (
		detectAgentIndex(line) !== -1 &&
		(l.includes('agent:') ||
			l.includes('# agent') ||
			l.includes('working agent'))
	)
		return 'log-agent-header';
	if (
		l.includes('agent:') ||
		l.includes('task:') ||
		l.includes('> entering') ||
		l.includes('crew')
	)
		return 'log-agent';
	if (
		l.includes('final answer') ||
		l.includes('completed') ||
		l.includes('finished')
	)
		return 'log-success';
	if (
		l.includes('thought:') ||
		l.includes('action:') ||
		l.includes('observation:')
	)
		return 'log-step';
	return '';
}

function encodeShare(str) {
	try { return btoa(unescape(encodeURIComponent(str))); } catch { return ''; }
}
function decodeShare(str) {
	try { return decodeURIComponent(escape(atob(str))); } catch { return null; }
}

function parseResult(content) {
	try {
		const parsed = JSON.parse(content.trim());
		const STRUCTURED_TYPES = ['chart', 'report', 'code', 'table', 'metrics'];
		if (STRUCTURED_TYPES.includes(parsed.output_type))
			return { type: 'structured', data: parsed };
		return { type: 'json', data: parsed };
	} catch {}
	const lines = content
		.trim()
		.split('\n')
		.filter((l) => l.trim());
	if (lines.length >= 2) {
		const cols = lines[0].split(',').length;
		if (
			cols > 1 &&
			lines.slice(1, 5).every((l) => l.split(',').length === cols)
		)
			return { type: 'csv', data: content };
	}
	return { type: 'text', data: content };
}

function triggerDownload(filename, content) {
	const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	a.click();
	URL.revokeObjectURL(url);
}

// ── Typewriter hook ───────────────────────────────────────────────────────────

function useTypewriter(text, speed = 10) {
	const [displayed, setDisplayed] = useState('');
	const [done, setDone] = useState(false);
	const prevRef = useRef('');

	useEffect(() => {
		if (!text) {
			setDisplayed('');
			setDone(false);
			return;
		}
		if (text === prevRef.current) return;
		prevRef.current = text;
		setDisplayed('');
		setDone(false);
		let i = 0;
		const id = setInterval(() => {
			i++;
			setDisplayed(text.slice(0, i));
			if (i >= text.length) {
				setDone(true);
				clearInterval(id);
			}
		}, speed);
		return () => clearInterval(id);
	}, [text, speed]);

	return { displayed, done };
}

// ── Pipeline diagram ──────────────────────────────────────────────────────────

function PipelineDiagram({ logs, status }) {
	const seen = useMemo(() => {
		const s = new Set();
		for (const line of logs) {
			const idx = detectAgentIndex(line);
			if (idx !== -1) s.add(idx);
		}
		return s;
	}, [logs]);

	const activeIdx = useMemo(() => {
		if (status !== 'running') return -1;
		for (let i = logs.length - 1; i >= 0; i--) {
			const idx = detectAgentIndex(logs[i]);
			if (idx !== -1) return idx;
		}
		return status === 'running' ? 0 : -1;
	}, [logs, status]);

	const items = [];
	PIPELINE_NODES.forEach((node, i) => {
		const isActive = activeIdx === i;
		const isDone =
			seen.has(i) && (status === 'done' || (activeIdx !== -1 && activeIdx > i));
		const isLit = isActive || isDone;
		items.push(
			<div
				key={node.id}
				className={`pipeline-node${isActive ? ' pipeline-node--active' : ''}${isDone ? ' pipeline-node--done' : ''}${!isLit ? ' pipeline-node--idle' : ''}`}
				style={{ '--nc': node.color }}>
				<span className='pipeline-node-icon'>{isDone ? '✓' : node.icon}</span>
				<span className='pipeline-node-label'>{node.label}</span>
			</div>,
		);
		if (i < PIPELINE_NODES.length - 1) {
			items.push(
				<div
					key={`conn-${i}`}
					className={`pipeline-conn${isLit ? ' pipeline-conn--lit' : ''}`}>
					<div className='pipeline-conn-line' />
					<div className='pipeline-conn-arrow'>›</div>
				</div>,
			);
		}
	});

	return <div className='pipeline'>{items}</div>;
}

// ── Download popup ────────────────────────────────────────────────────────────

function DownloadPopup({ parsed, onClose }) {
	const options = [];
	if (parsed.type === 'structured') {
		const d = parsed.data;
		const reportMd = [
			`# ${d.chart_title || 'Analysis Report'}`,
			`\n## Summary\n${d.summary}`,
			`\n## Findings\n${d.findings.map((f) => `- ${f}`).join('\n')}`,
			`\n## Recommendations\n${d.recommendations.map((r) => `- ${r}`).join('\n')}`,
		].join('\n');
		options.push({ icon: '{}', label: 'Full JSON', ext: 'json', content: JSON.stringify(d, null, 2) });
		options.push({ icon: '📝', label: 'Report (Markdown)', ext: 'md', content: reportMd });
		if (d.data_points?.length) {
			const csv = ['label,value,category,x_value', ...d.data_points.map((p) => `${p.label},${p.value},${p.category ?? ''},${p.x_value ?? ''}`)];
			options.push({ icon: '⊞', label: 'Chart Data (CSV)', ext: 'csv', content: csv.join('\n') });
		}
		if (d.code_blocks?.length) {
			const ext = { python: 'py', sql: 'sql', bash: 'sh', r: 'r', javascript: 'js' };
			d.code_blocks.forEach((b, i) => {
				options.push({ icon: '{ }', label: `Code: ${b.title}`, ext: ext[b.language] ?? 'txt', content: b.code });
			});
		}
		if (d.table_headers?.length) {
			const csv = [d.table_headers.join(','), ...d.table_rows.map((r) => r.join(','))];
			options.push({ icon: '⊞', label: 'Table (CSV)', ext: 'csv', content: csv.join('\n') });
		}
		if (d.metrics?.length) {
			const csv = ['label,value,unit,trend,change,context', ...d.metrics.map((m) => `${m.label},${m.value},${m.unit ?? ''},${m.trend ?? ''},${m.change ?? ''},${m.context ?? ''}`)];
			options.push({ icon: '◈', label: 'Metrics (CSV)', ext: 'csv', content: csv.join('\n') });
		}
		if (d.comparison_rows?.length) {
			const csv = [`metric,${d.comparison_a_label || 'A'},${d.comparison_b_label || 'B'},winner`,
				...d.comparison_rows.map((r) => `${r.metric},${r.value_a},${r.value_b},${r.winner ?? ''}`)];
			options.push({ icon: '⇌', label: 'Comparison (CSV)', ext: 'csv', content: csv.join('\n') });
		}
		if (d.heatmap_values?.length) {
			const header = ['', ...(d.heatmap_col_labels ?? [])].join(',');
			const dataRows = (d.heatmap_row_labels ?? []).map((r, i) =>
				[r, ...(d.heatmap_values[i] ?? [])].join(',')
			);
			options.push({ icon: '▦', label: 'Heatmap (CSV)', ext: 'csv', content: [header, ...dataRows].join('\n') });
		}
	} else if (parsed.type === 'json') {
		options.push({
			icon: '{}',
			label: 'JSON',
			ext: 'json',
			content: JSON.stringify(parsed.data, null, 2),
		});
	} else if (parsed.type === 'csv') {
		options.push({ icon: '⊞', label: 'CSV', ext: 'csv', content: parsed.data });
	}
	options.push({
		icon: '📄',
		label: 'Plain Text',
		ext: 'txt',
		content:
			typeof parsed.data === 'string'
				? parsed.data
				: JSON.stringify(parsed.data, null, 2),
	});

	return (
		<div
			className='popup-overlay'
			onClick={onClose}>
			<div
				className='popup'
				onClick={(e) => e.stopPropagation()}>
				<div className='popup-header'>
					<span>Download Result</span>
					<button
						className='popup-close'
						onClick={onClose}>
						×
					</button>
				</div>
				<div className='popup-body'>
					{options.map((opt, i) => (
						<button
							key={i}
							className='popup-option'
							onClick={() => {
								triggerDownload(`result.${opt.ext}`, opt.content);
								onClose();
							}}>
							<span className='popup-option-icon'>{opt.icon}</span>
							<div className='popup-option-info'>
								<span className='popup-option-label'>{opt.label}</span>
								<span className='popup-option-ext'>.{opt.ext}</span>
							</div>
						</button>
					))}
				</div>
			</div>
		</div>
	);
}

// ── Chart renderer ────────────────────────────────────────────────────────────

function ChartView({ data, overrideType }) {
	const points = data.data_points ?? [];
	const chartType = overrideType || data.chart_type;
	const chartData = points.map((p) => ({
		name: p.label,
		value: p.value,
		x: p.x_value ?? null,
		category: p.category,
	}));
	const tooltipStyle = {
		background: '#13141a',
		border: '1px solid #1e2030',
		borderRadius: 8,
		fontSize: 12,
	};

	if (chartType === 'pie') {
		return (
			<ResponsiveContainer width='100%' height={300}>
				<PieChart>
					<Pie
						data={chartData}
						dataKey='value'
						nameKey='name'
						cx='50%'
						cy='50%'
						outerRadius={110}
						label={({ name, percent }) =>
							`${name} ${(percent * 100).toFixed(0)}%`
						}
						labelLine={false}>
						{chartData.map((_, i) => (
							<Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
						))}
					</Pie>
					<Tooltip contentStyle={tooltipStyle} />
					<Legend wrapperStyle={{ fontSize: 12, color: '#6b7280' }} />
				</PieChart>
			</ResponsiveContainer>
		);
	}

	if (chartType === 'scatter') {
		const scatterData = chartData.map((p) => ({
			x: p.x ?? 0,
			y: p.value,
			name: p.name,
		}));
		return (
			<ResponsiveContainer width='100%' height={300}>
				<ScatterChart margin={{ top: 10, right: 20, bottom: 20, left: 0 }}>
					<CartesianGrid strokeDasharray='3 3' stroke='#1e2030' />
					<XAxis
						dataKey='x'
						type='number'
						name={data.x_axis_label || 'X'}
						tick={{ fill: '#4a4f6a', fontSize: 11 }}
						label={{ value: data.x_axis_label, position: 'insideBottom', offset: -10, fill: '#3d405a', fontSize: 11 }}
					/>
					<YAxis
						dataKey='y'
						type='number'
						name={data.y_axis_label || 'Y'}
						tick={{ fill: '#4a4f6a', fontSize: 11 }}
						label={{ value: data.y_axis_label, angle: -90, position: 'insideLeft', fill: '#3d405a', fontSize: 11 }}
					/>
					<ZAxis range={[40, 40]} />
					<Tooltip
						contentStyle={tooltipStyle}
						cursor={{ strokeDasharray: '3 3' }}
						content={({ payload }) => {
							if (!payload?.length) return null;
							const d = payload[0].payload;
							return (
								<div style={tooltipStyle} className='scatter-tip'>
									<p style={{ color: '#e2e8f0', margin: 0 }}>{d.name}</p>
									<p style={{ color: '#6366f1', margin: 0 }}>{data.x_axis_label}: {d.x}</p>
									<p style={{ color: '#34d399', margin: 0 }}>{data.y_axis_label}: {d.y}</p>
								</div>
							);
						}}
					/>
					<Scatter data={scatterData} fill='#6366f1' fillOpacity={0.8} />
				</ScatterChart>
			</ResponsiveContainer>
		);
	}

	if (chartType === 'line') {
		return (
			<ResponsiveContainer width='100%' height={300}>
				<LineChart data={chartData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
					<CartesianGrid strokeDasharray='3 3' stroke='#1e2030' />
					<XAxis dataKey='name' tick={{ fill: '#4a4f6a', fontSize: 11 }} />
					<YAxis
						tick={{ fill: '#4a4f6a', fontSize: 11 }}
						label={{ value: data.y_axis_label, angle: -90, position: 'insideLeft', fill: '#3d405a', fontSize: 11 }}
					/>
					<Tooltip contentStyle={tooltipStyle} />
					<Line
						type='monotone'
						dataKey='value'
						stroke='#6366f1'
						strokeWidth={2}
						dot={{ fill: '#6366f1', r: 4 }}
						activeDot={{ r: 6 }}
					/>
				</LineChart>
			</ResponsiveContainer>
		);
	}

	if (chartType === 'funnel') {
		const funnelData = chartData.map((d, i) => ({
			...d,
			fill: CHART_COLORS[i % CHART_COLORS.length],
		}));
		return (
			<ResponsiveContainer width='100%' height={300}>
				<FunnelChart>
					<Funnel dataKey='value' data={funnelData} isAnimationActive>
						<LabelList position='center' fill='#fff' fontSize={11} dataKey='name' />
					</Funnel>
					<Tooltip contentStyle={tooltipStyle} />
				</FunnelChart>
			</ResponsiveContainer>
		);
	}

	if (chartType === 'radar') {
		const radarData = points.map((p) => ({
			subject: p.label,
			value: p.value,
			value2: p.value2 ?? null,
		}));
		const hasB = radarData.some((d) => d.value2 != null);
		return (
			<ResponsiveContainer width='100%' height={300}>
				<RadarChart cx='50%' cy='50%' outerRadius={100} data={radarData}>
					<PolarGrid stroke='#1e2030' />
					<PolarAngleAxis dataKey='subject' tick={{ fill: '#4a4f6a', fontSize: 11 }} />
					<PolarRadiusAxis tick={{ fill: '#3d405a', fontSize: 9 }} />
					<Radar
						name={data.chart_title || 'Series A'}
						dataKey='value'
						stroke='#6366f1'
						fill='#6366f1'
						fillOpacity={0.35}
					/>
					{hasB && (
						<Radar
							name={data.radar_b_label || 'Series B'}
							dataKey='value2'
							stroke='#34d399'
							fill='#34d399'
							fillOpacity={0.2}
						/>
					)}
					<Legend wrapperStyle={{ fontSize: 12, color: '#6b7280' }} />
					<Tooltip contentStyle={tooltipStyle} />
				</RadarChart>
			</ResponsiveContainer>
		);
	}

	// Default: bar
	return (
		<ResponsiveContainer width='100%' height={300}>
			<BarChart data={chartData} margin={{ top: 5, right: 20, bottom: 30, left: 0 }}>
				<CartesianGrid strokeDasharray='3 3' stroke='#1e2030' vertical={false} />
				<XAxis
					dataKey='name'
					tick={{ fill: '#4a4f6a', fontSize: 11 }}
					angle={-30}
					textAnchor='end'
					interval={0}
				/>
				<YAxis
					tick={{ fill: '#4a4f6a', fontSize: 11 }}
					label={{ value: data.y_axis_label, angle: -90, position: 'insideLeft', fill: '#3d405a', fontSize: 11 }}
				/>
				<Tooltip contentStyle={tooltipStyle} cursor={{ fill: 'rgba(99,102,241,0.06)' }} />
				<Bar dataKey='value' radius={[4, 4, 0, 0]}>
					{chartData.map((_, i) => (
						<Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
					))}
				</Bar>
			</BarChart>
		</ResponsiveContainer>
	);
}

// ── Data table for chart points ───────────────────────────────────────────────

function DataPointTable({ data }) {
	const points = data.data_points ?? [];
	const hasCategory = points.some((p) => p.category);
	const hasX = points.some((p) => p.x_value != null);
	return (
		<div className='result-table-wrap'>
			<table className='result-table'>
				<thead>
					<tr>
						<th>{data.x_axis_label || 'Label'}</th>
						{hasX && <th>X Value</th>}
						<th>{data.y_axis_label || 'Value'}</th>
						{hasCategory && <th>Category</th>}
					</tr>
				</thead>
				<tbody>
					{points.map((p, i) => (
						<tr key={i}>
							<td>{p.label}</td>
							{hasX && <td>{p.x_value?.toLocaleString() ?? '—'}</td>}
							<td>{p.value.toLocaleString()}</td>
							{hasCategory && <td>{p.category ?? '—'}</td>}
						</tr>
					))}
				</tbody>
			</table>
		</div>
	);
}

// ── Result views ──────────────────────────────────────────────────────────────

function StructuredReport({ data }) {
	const { displayed: sumDisplayed, done: sumDone } = useTypewriter(data.summary, 10);
	const hasChart = data.output_type === 'chart' && data.data_points?.length > 0;
	const hasCode = data.output_type === 'code' && data.code_blocks?.length > 0;
	const hasMetrics = data.output_type === 'metrics' && data.metrics?.length > 0;
	const hasTable = data.output_type === 'table' && data.table_headers?.length > 0;
	const hasComparison = data.output_type === 'comparison' && data.comparison_rows?.length > 0;
	const hasHeatmap = data.output_type === 'heatmap' && data.heatmap_values?.length > 0;

	return (
		<div className='structured-report'>
			{hasChart && (
				<div className='report-chart-inline'>
					{data.chart_title && <h3 className='chart-title chart-title--inline'>{data.chart_title}</h3>}
					<ChartView data={data} />
				</div>
			)}
			{hasMetrics && (
				<div className='report-chart-inline'>
					<MetricsView data={data} />
				</div>
			)}
			{hasCode && (
				<div className='report-chart-inline'>
					<CodeView data={data} />
				</div>
			)}
			{hasTable && (
				<div className='report-chart-inline'>
					<TableOutputView data={data} />
				</div>
			)}
			{hasComparison && (
				<div className='report-chart-inline'>
					<ComparisonView data={data} />
				</div>
			)}
			{hasHeatmap && (
				<div className='report-chart-inline'>
					<HeatmapView data={data} />
				</div>
			)}
			<div className='report-summary'>
				{sumDisplayed}
				{!sumDone && <span className='typewriter-cursor' />}
			</div>
			<div className={`report-sections${sumDone ? ' report-sections--visible' : ''}`}>
				{data.findings?.length > 0 && (
					<div className='report-section'>
						<h3 className='report-section-title'>Findings</h3>
						<ul className='report-list'>
							{data.findings.map((f, i) => (
								<li key={i}>
									<ReactMarkdown remarkPlugins={[remarkGfm]}>{f}</ReactMarkdown>
								</li>
							))}
						</ul>
					</div>
				)}
				{data.recommendations?.length > 0 && (
					<div className='report-section'>
						<h3 className='report-section-title'>Recommendations</h3>
						<ul className='report-list report-list--rec'>
							{data.recommendations.map((r, i) => (
								<li key={i}>
									<ReactMarkdown remarkPlugins={[remarkGfm]}>{r}</ReactMarkdown>
								</li>
							))}
						</ul>
					</div>
				)}
			</div>
		</div>
	);
}

function ResultView({ parsed }) {
	if (parsed.type === 'structured')
		return <StructuredReport data={parsed.data} />;
	if (parsed.type === 'json')
		return (
			<pre className='result-code result-json'>
				{JSON.stringify(parsed.data, null, 2)}
			</pre>
		);
	if (parsed.type === 'csv') {
		const lines = parsed.data
			.trim()
			.split('\n')
			.filter((l) => l.trim());
		const headers = lines[0]
			.split(',')
			.map((h) => h.trim().replace(/^"|"$/g, ''));
		const rows = lines
			.slice(1)
			.map((l) => l.split(',').map((c) => c.trim().replace(/^"|"$/g, '')));
		return (
			<div className='result-table-wrap'>
				<table className='result-table'>
					<thead>
						<tr>
							{headers.map((h, i) => (
								<th key={i}>{h}</th>
							))}
						</tr>
					</thead>
					<tbody>
						{rows.map((row, i) => (
							<tr key={i}>
								{row.map((c, j) => (
									<td key={j}>{c}</td>
								))}
							</tr>
						))}
					</tbody>
				</table>
			</div>
		);
	}
	return (
		<div className='result-markdown'>
			<ReactMarkdown remarkPlugins={[remarkGfm]}>{parsed.data}</ReactMarkdown>
		</div>
	);
}

// ── History drawer ────────────────────────────────────────────────────────────

const TYPE_COLORS = {
	chart: '#a78bfa',
	report: '#34d399',
	json: '#60a5fa',
	csv: '#fbbf24',
	text: '#94a3b8',
};

function HistoryDrawer({ history, onSelect, onClose }) {
	return (
		<div
			className='popup-overlay'
			onClick={onClose}>
			<div
				className='history-drawer'
				onClick={(e) => e.stopPropagation()}>
				<div className='popup-header'>
					<span>Session History</span>
					<button
						className='popup-close'
						onClick={onClose}>
						×
					</button>
				</div>
				<div className='history-list'>
					{history.length === 0 ? (
						<div className='history-empty'>
							No saved sessions yet. Complete an analysis to save it.
						</div>
					) : (
						history.map((entry) => {
							const c = TYPE_COLORS[entry.resultType] || '#94a3b8';
							return (
								<button
									key={entry.id}
									className='history-item'
									onClick={() => onSelect(entry)}>
									<div className='history-item-top'>
										<span
											className='history-item-badge'
											style={{
												color: c,
												borderColor: c + '44',
												background: c + '11',
											}}>
											{(entry.resultType || 'unknown').toUpperCase()}
										</span>
										<span className='history-item-time'>
											{timeAgo(entry.timestamp)}
										</span>
									</div>
									<p className='history-item-context'>
										{entry.context.slice(0, 90)}
										{entry.context.length > 90 ? '…' : ''}
									</p>
									<div className='history-item-meta'>
										{entry.logCount} log lines
										{entry.fileName ? ` · ${entry.fileName}` : ''}
									</div>
								</button>
							);
						})
					)}
				</div>
			</div>
		</div>
	);
}

// ── Code view ─────────────────────────────────────────────────────────────────

const LANG_COLORS = {
	python: '#3b82f6',
	sql: '#f59e0b',
	bash: '#34d399',
	r: '#a78bfa',
	javascript: '#fbbf24',
};

function CodeView({ data }) {
	const blocks = data.code_blocks ?? [];
	const [copied, setCopied] = useState(null);

	function copyBlock(code, i) {
		navigator.clipboard.writeText(code).then(() => {
			setCopied(i);
			setTimeout(() => setCopied(null), 1500);
		});
	}

	return (
		<div className='code-view'>
			{blocks.map((block, i) => (
				<div key={i} className='code-block'>
					<div className='code-block-header'>
						<span
							className='code-lang-badge'
							style={{ color: LANG_COLORS[block.language] ?? '#94a3b8', borderColor: (LANG_COLORS[block.language] ?? '#94a3b8') + '44', background: (LANG_COLORS[block.language] ?? '#94a3b8') + '11' }}>
							{block.language}
						</span>
						<span className='code-block-title'>{block.title}</span>
						<button className='code-copy-btn' onClick={() => copyBlock(block.code, i)}>
							{copied === i ? '✓ copied' : 'copy'}
						</button>
					</div>
					<pre className='code-pre'><code>{block.code}</code></pre>
				</div>
			))}
		</div>
	);
}

// ── Metrics view ──────────────────────────────────────────────────────────────

const TREND_ICON = { up: '↑', down: '↓', neutral: '→' };
const TREND_COLOR = { up: '#34d399', down: '#f87171', neutral: '#6b7280' };

function MetricsView({ data }) {
	const items = data.metrics ?? [];
	return (
		<div className='metrics-grid'>
			{items.map((m, i) => {
				const tc = TREND_COLOR[m.trend] ?? '#6b7280';
				const ti = TREND_ICON[m.trend] ?? '';
				return (
					<div key={i} className='metric-card'>
						<span className='metric-label'>{m.label}</span>
						<div className='metric-value-row'>
							<span className='metric-value'>{m.value}</span>
							{m.unit && <span className='metric-unit'>{m.unit}</span>}
						</div>
						{(m.trend || m.change) && (
							<div className='metric-trend' style={{ color: tc }}>
								{ti && <span>{ti}</span>}
								{m.change && <span>{m.change}</span>}
								{m.context && <span className='metric-context'>{m.context}</span>}
							</div>
						)}
					</div>
				);
			})}
		</div>
	);
}

// ── Table output view ─────────────────────────────────────────────────────────

function TableOutputView({ data }) {
	const headers = data.table_headers ?? [];
	const rows = data.table_rows ?? [];
	return (
		<div className='result-table-wrap'>
			<table className='result-table'>
				<thead>
					<tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr>
				</thead>
				<tbody>
					{rows.map((row, i) => (
						<tr key={i}>
							{row.map((cell, j) => <td key={j}>{cell}</td>)}
						</tr>
					))}
				</tbody>
			</table>
		</div>
	);
}

// ── Comparison view ───────────────────────────────────────────────────────────

function ComparisonView({ data }) {
	const rows = data.comparison_rows ?? [];
	const aLabel = data.comparison_a_label || 'A';
	const bLabel = data.comparison_b_label || 'B';
	const aWins = rows.filter((r) => r.winner === 'a').length;
	const bWins = rows.filter((r) => r.winner === 'b').length;
	return (
		<div className='comparison-view'>
			<div className='comparison-header'>
				<div className='comparison-cell comparison-cell--metric' />
				<div className='comparison-cell comparison-cell--a'>{aLabel}</div>
				<div className='comparison-cell comparison-cell--b'>{bLabel}</div>
			</div>
			{rows.map((row, i) => (
				<div key={i} className='comparison-row'>
					<div className='comparison-cell comparison-cell--metric'>{row.metric}</div>
					<div className={`comparison-cell comparison-cell--a${row.winner === 'a' ? ' comparison-cell--winner' : ''}`}>
						{row.winner === 'a' && <span className='winner-badge'>▲</span>}
						{row.value_a}
					</div>
					<div className={`comparison-cell comparison-cell--b${row.winner === 'b' ? ' comparison-cell--winner' : ''}`}>
						{row.winner === 'b' && <span className='winner-badge'>▲</span>}
						{row.value_b}
					</div>
				</div>
			))}
			{rows.length > 0 && (
				<div className='comparison-footer'>
					<span style={{ color: '#6366f1' }}>{aLabel}: {aWins} wins</span>
					<span style={{ color: '#34d399' }}>{bLabel}: {bWins} wins</span>
					{rows.length - aWins - bWins > 0 && (
						<span style={{ color: '#4a4f6a' }}>{rows.length - aWins - bWins} ties</span>
					)}
				</div>
			)}
		</div>
	);
}

// ── Heatmap view ──────────────────────────────────────────────────────────────

function HeatmapView({ data }) {
	const rows = data.heatmap_row_labels ?? [];
	const cols = data.heatmap_col_labels ?? [];
	const values = data.heatmap_values ?? [];
	const flat = values.flat().filter((v) => typeof v === 'number');
	const min = flat.length ? Math.min(...flat) : 0;
	const max = flat.length ? Math.max(...flat) : 1;

	function colorFor(v) {
		const t = max === min ? 0.5 : (v - min) / (max - min);
		const r = Math.round(60 + t * 190);
		const g = Math.round(70 - t * 30);
		const b = Math.round(241 - t * 180);
		return `rgba(${r},${g},${b},${0.3 + t * 0.6})`;
	}

	return (
		<div className='heatmap-wrap'>
			{data.heatmap_title && <h3 className='heatmap-title'>{data.heatmap_title}</h3>}
			<div
				className='heatmap-grid'
				style={{ gridTemplateColumns: `auto repeat(${cols.length}, 1fr)` }}>
				<div className='heatmap-cell heatmap-cell--corner' />
				{cols.map((c, j) => (
					<div key={j} className='heatmap-cell heatmap-cell--col-label'>{c}</div>
				))}
				{rows.map((r, i) => (
					<>
						<div key={`rl${i}`} className='heatmap-cell heatmap-cell--row-label'>{r}</div>
						{cols.map((_, j) => {
							const v = values[i]?.[j] ?? 0;
							return (
								<div
									key={j}
									className='heatmap-cell heatmap-cell--value'
									title={`${r} × ${cols[j]}: ${v}`}
									style={{ background: colorFor(v) }}>
									{v.toLocaleString(undefined, { maximumFractionDigits: 1 })}
								</div>
							);
						})}
					</>
				))}
			</div>
		</div>
	);
}

// ── Artifacts panel (full chart view + type switcher + data table) ────────────

const CHART_TYPES = [
	{ id: 'bar', label: '▬ Bar' },
	{ id: 'line', label: '╱ Line' },
	{ id: 'pie', label: '◔ Pie' },
	{ id: 'scatter', label: '⁘ Scatter' },
	{ id: 'funnel', label: '⯆ Funnel' },
	{ id: 'radar', label: '⬡ Radar' },
];

function ArtifactsPanel({ parsed, onDownload }) {
	const data = parsed.data;
	const otype = data.output_type;

	// chart-specific state
	const [viewType, setViewType] = useState(data.chart_type || 'bar');
	const [artifactTab, setArtifactTab] = useState(
		otype === 'code' ? 'code'
		: otype === 'metrics' ? 'metrics'
		: otype === 'table' ? 'table'
		: otype === 'comparison' ? 'comparison'
		: otype === 'heatmap' ? 'heatmap'
		: 'chart'
	);
	const hasScatterData = data.data_points?.some((p) => p.x_value != null);
	const hasRadarB = data.data_points?.some((p) => p.value2 != null);
	const availableTypes = CHART_TYPES.filter((t) => {
		if (t.id === 'scatter') return hasScatterData;
		if (t.id === 'radar') return (data.data_points?.length ?? 0) >= 3;
		if (t.id === 'pie') return (data.data_points?.length ?? 0) <= 6;
		if (t.id === 'funnel') return (data.data_points?.length ?? 0) >= 2;
		return true;
	});

	const badgeLabel = {
		chart: 'CHART', code: 'CODE', table: 'TABLE',
		metrics: 'METRICS', comparison: 'COMPARE', heatmap: 'HEATMAP',
	}[otype] ?? otype.toUpperCase();
	const footerInfo = otype === 'chart' ? `${data.data_points?.length} data points · ${viewType} view`
		: otype === 'code' ? `${data.code_blocks?.length} block(s)`
		: otype === 'table' ? `${data.table_rows?.length} rows · ${data.table_headers?.length} cols`
		: otype === 'metrics' ? `${data.metrics?.length} metrics`
		: otype === 'comparison' ? `${data.comparison_rows?.length} metrics compared`
		: otype === 'heatmap' ? `${data.heatmap_row_labels?.length} × ${data.heatmap_col_labels?.length} matrix`
		: '';

	return (
		<div className='result-panel'>
			<div className='result-type-bar'>
				<span className='result-type-badge'>{badgeLabel}</span>
				{otype === 'chart' && (
					<div className='chart-type-switcher'>
						{availableTypes.map((t) => (
							<button
								key={t.id}
								className={`chart-type-btn${viewType === t.id ? ' chart-type-btn--active' : ''}`}
								onClick={() => setViewType(t.id)}>
								{t.label}
							</button>
						))}
					</div>
				)}
			</div>

			{otype === 'chart' && (
				<div className='artifact-sub-tabs'>
					{['chart', 'table', 'stats'].map((tab) => (
						<button key={tab} className={`artifact-sub-tab${artifactTab === tab ? ' artifact-sub-tab--active' : ''}`} onClick={() => setArtifactTab(tab)}>
							{tab.charAt(0).toUpperCase() + tab.slice(1)}
						</button>
					))}
				</div>
			)}

			<div className='artifact-body'>
				{otype === 'code' && <CodeView data={data} />}
				{otype === 'metrics' && <MetricsView data={data} />}
				{otype === 'table' && <TableOutputView data={data} />}
				{otype === 'comparison' && <ComparisonView data={data} />}
				{otype === 'heatmap' && <HeatmapView data={data} />}
				{otype === 'chart' && (
					<>
						{artifactTab === 'chart' && (
							<>
								{data.chart_title && <h2 className='chart-title'>{data.chart_title}</h2>}
								{data.x_axis_label && <p className='chart-axis-label'>X: {data.x_axis_label} · Y: {data.y_axis_label}</p>}
								<ChartView data={data} overrideType={viewType} />
							</>
						)}
						{artifactTab === 'table' && <DataPointTable data={data} />}
						{artifactTab === 'stats' && (
							<div className='artifact-stats'>
								{data.data_points.map((p, i) => (
									<div key={i} className='stat-chip'
										style={{ borderColor: CHART_COLORS[i % CHART_COLORS.length] + '44', background: CHART_COLORS[i % CHART_COLORS.length] + '11' }}>
										<span className='stat-label'>{p.label}</span>
										<span className='stat-value' style={{ color: CHART_COLORS[i % CHART_COLORS.length] }}>{p.value.toLocaleString()}</span>
										{p.category && <span className='stat-category'>{p.category}</span>}
									</div>
								))}
							</div>
						)}
					</>
				)}
			</div>
			<div className='log-footer'>
				<span>{footerInfo}</span>
				<button className='btn-download' onClick={onDownload}>⬇ Download</button>
			</div>
		</div>
	);
}

// ── File preview panel ────────────────────────────────────────────────────────

function FilePreviewPanel({ previews, files }) {
	const [expanded, setExpanded] = useState(true);
	const entries = files.filter((f) => previews[f.name]);
	if (entries.length === 0) return null;
	return (
		<div className='file-preview-panel'>
			<div className='file-preview-header' onClick={() => setExpanded((v) => !v)}>
				<span>File Preview</span>
				<span className='file-preview-toggle'>{expanded ? '▾' : '▸'}</span>
			</div>
			{expanded && entries.map((f, i) => (
				<div key={i} className='file-preview-item'>
					<div className='file-preview-name'>{f.name}</div>
					<pre className='file-preview-content'>{previews[f.name]}</pre>
				</div>
			))}
		</div>
	);
}

// ── Main app ──────────────────────────────────────────────────────────────────

export default function App() {
	const [context, setContext] = useState('');
	const [files, setFiles] = useState([]);
	const [logs, setLogs] = useState([]);
	const [result, setResult] = useState(null);
	const [status, setStatus] = useState('idle');
	const [showLog, setShowLog] = useState(true);
	const [activeTab, setActiveTab] = useState('logs');
	const [showDownload, setShowDownload] = useState(false);
	const [showHistory, setShowHistory] = useState(false);
	const [history, setHistory] = useState(() => loadHistory());
	const [filePreviews, setFilePreviews] = useState({});
	const [shareCopied, setShareCopied] = useState(false);
	const logEndRef = useRef(null);
	const fileInputRef = useRef(null);
	const abortRef = useRef(null);
	const analyzeRef = useRef(null);

	useEffect(() => {
		if (activeTab === 'logs')
			logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
	}, [logs, activeTab]);

	// Load shared result from URL hash on mount
	useEffect(() => {
		const hash = window.location.hash;
		if (hash.startsWith('#r=')) {
			const decoded = decodeShare(hash.slice(3));
			if (decoded) {
				setResult(decoded);
				setStatus('done');
				setActiveTab('result');
			}
		}
	}, []);

	// Read file previews when files change
	useEffect(() => {
		if (files.length === 0) { setFilePreviews({}); return; }
		files.forEach((f) => {
			if (filePreviews[f.name]) return;
			if (f.type === 'application/pdf') {
				setFilePreviews((p) => ({ ...p, [f.name]: '(PDF — preview not available)' }));
				return;
			}
			f.text()
				.then((text) => {
					const lines = text.split('\n').slice(0, 8).join('\n');
					const preview = lines.length > 500 ? lines.slice(0, 500) + '…' : lines;
					setFilePreviews((p) => ({ ...p, [f.name]: preview }));
				})
				.catch(() => setFilePreviews((p) => ({ ...p, [f.name]: '(preview unavailable)' })));
		});
	}, [files]); // eslint-disable-line

	// Keyboard shortcuts
	useEffect(() => {
		function onKeyDown(e) {
			if (e.key === 'Escape') {
				setShowDownload(false);
				setShowHistory(false);
			}
			if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
				e.preventDefault();
				analyzeRef.current?.();
			}
		}
		window.addEventListener('keydown', onKeyDown);
		return () => window.removeEventListener('keydown', onKeyDown);
	}, []);

	useEffect(() => {
		if (result) setActiveTab('result');
	}, [result]);

	// Save completed run to history
	useEffect(() => {
		if (status !== 'done' || !result) return;
		const p = parseResult(result);
		const entry = {
			id: Date.now(),
			timestamp: new Date().toISOString(),
			context,
			fileName: files.length > 0 ? files.map((f) => f.name).join(', ') : null,
			result,
			logCount: logs.length,
			resultType: p.type === 'structured' ? p.data.output_type : p.type,
		};
		const updated = [entry, ...history].slice(0, MAX_HISTORY);
		setHistory(updated);
		saveHistory(updated);
	}, [status]); // eslint-disable-line

	const parsed = result ? parseResult(result) : null;
	const hasChart =
		parsed?.type === 'structured' &&
		parsed.data?.output_type === 'chart' &&
		parsed.data?.data_points?.length > 0;
	const hasArtifacts =
		parsed?.type === 'structured' &&
		['chart', 'code', 'table', 'metrics', 'comparison', 'heatmap'].includes(parsed.data?.output_type);

	async function handleAnalyze(isAutoRetry = false) {
		if (!context.trim() || (status === 'running' && !isAutoRetry)) return;
		const controller = new AbortController();
		abortRef.current = controller;

		if (isAutoRetry) {
			setLogs((prev) => [
				...prev,
				'[RETRY]  All providers rate-limited. Auto-retrying in 10s…',
			]);
			await new Promise((r) => setTimeout(r, 10000));
			if (controller.signal.aborted) return;
		}
		setLogs(isAutoRetry ? (prev) => [...prev] : []);
		setResult(null);
		setActiveTab('logs');
		setStatus('running');
		setShowLog(true);

		const form = new FormData();
		form.append('context', context);
		files.forEach((f) => form.append('files', f));
		let gotResult = false;

		try {
			const res = await fetch(`${API}/analyze`, {
				method: 'POST',
				body: form,
				signal: controller.signal,
			});
			if (!res.ok) {
				let msg = `Server returned ${res.status}`;
				try {
					const j = await res.json();
					if (j.error) msg = j.error;
				} catch {}
				setLogs((prev) => [...prev, `[ERROR] ${msg}`]);
				setStatus('error');
				return;
			}

			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buf = '';

			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buf += decoder.decode(value, { stream: true });
				const lines = buf.split('\n');
				buf = lines.pop();
				for (const raw of lines) {
					if (!raw.startsWith('data: ')) continue;
					let payload;
					try {
						payload = JSON.parse(raw.slice(6));
					} catch {
						continue;
					}
					if (payload === '__DONE__') {
						if (!gotResult && !isAutoRetry) {
							// All rotations exhausted — auto-retry once
							handleAnalyze(true);
						} else {
							setStatus(gotResult ? 'done' : 'error');
						}
						return;
					}
					if (payload?.type === 'result') {
						gotResult = true;
						setResult(payload.content);
					} else {
						setLogs((prev) => [...prev, payload]);
					}
				}
			}
			setStatus('done');
		} catch (err) {
			if (err.name !== 'AbortError') {
				setLogs((prev) => [...prev, `[ERROR] ${err.message}`]);
				setStatus('error');
			}
		}
	}

	// Keep ref in sync so keyboard shortcut always calls latest version
	analyzeRef.current = handleAnalyze;

	function shareResult() {
		if (!result) return;
		const encoded = encodeShare(result);
		const base = window.location.href.split('#')[0];
		const url = base + '#r=' + encoded;
		window.location.hash = 'r=' + encoded;
		navigator.clipboard.writeText(url).then(() => {
			setShareCopied(true);
			setTimeout(() => setShareCopied(false), 2000);
		});
	}

	function handleReset() {
		setLogs([]);
		setResult(null);
		setStatus('idle');
		setActiveTab('logs');
		setFiles([]);
		setFilePreviews({});
		setContext('');
		if (fileInputRef.current) fileInputRef.current.value = '';
		window.location.hash = '';
	}

	function handleHistorySelect(entry) {
		setContext(entry.context);
		setResult(entry.result);
		setLogs([]);
		setStatus('done');
		setActiveTab('result');
		setFiles([]);
		setShowHistory(false);
	}

	return (
		<div className='app'>
			<header className='topbar'>
				<div className='topbar-brand'>
					<svg
						width='20'
						height='20'
						viewBox='0 0 20 20'
						fill='none'
						aria-hidden='true'>
						<polygon
							points='10,1 18,5.5 18,14.5 10,19 2,14.5 2,5.5'
							stroke='currentColor'
							strokeWidth='1.5'
							fill='none'
						/>
						<circle
							cx='10'
							cy='10'
							r='2.5'
							fill='currentColor'
						/>
					</svg>
					<span>DataFlow</span>
				</div>
				<div className='topbar-right'>
					<span className='topbar-tag'>AI Agent Pipeline</span>
					<button
						className='toggle-log-btn'
						onClick={() => setShowHistory(true)}>
						History
						{history.length > 0 && (
							<span className='toggle-log-count'>{history.length}</span>
						)}
					</button>
					<button
						className={`toggle-log-btn${showLog ? ' toggle-log-btn--active' : ''}`}
						onClick={() => setShowLog((v) => !v)}>
						<svg
							width='16'
							height='16'
							viewBox='0 0 16 16'
							fill='none'>
							<rect
								x='1'
								y='2'
								width='14'
								height='12'
								rx='2'
								stroke='currentColor'
								strokeWidth='1.3'
							/>
							<line
								x1='6'
								y1='2'
								x2='6'
								y2='14'
								stroke='currentColor'
								strokeWidth='1.3'
							/>
						</svg>
						{showLog ? 'Hide Output' : 'Show Output'}
						{!showLog && logs.length > 0 && (
							<span className='toggle-log-count'>{logs.length}</span>
						)}
					</button>
				</div>
			</header>

			<main className={`workspace${showLog ? '' : ' workspace--log-hidden'}`}>
				<section className='input-panel'>
					<h1 className='panel-title'>What do you want to analyze?</h1>
					<p className='panel-sub'>
						Describe your dataset and the question you want answered. The agent
						pipeline will interpret your request, clean the data, and extract
						insights.
					</p>

					<div className='field'>
						<label className='field-label'>Context</label>
						<textarea
							className='context-area'
							placeholder='e.g. I have a CSV of monthly sales. Show me the top 5 products by revenue in Q3 and flag any anomalies…'
							value={context}
							onChange={(e) => setContext(e.target.value)}
							rows={5}
							disabled={status === 'running'}
						/>
					</div>

					<div className='field'>
						<label className='field-label'>Data files <span className='field-label-sub'>(up to 3)</span></label>
						<div
							className={`dropzone${files.length > 0 ? ' dropzone--has-file' : ''}`}
							onDrop={(e) => {
								e.preventDefault();
								const dropped = Array.from(e.dataTransfer.files).slice(0, 3);
								setFiles((prev) => [...prev, ...dropped].slice(0, 3));
							}}
							onDragOver={(e) => e.preventDefault()}
							onClick={() => fileInputRef.current?.click()}
							role='button'
							tabIndex={0}
							onKeyDown={(e) => e.key === 'Enter' && fileInputRef.current?.click()}>
							<input
								ref={fileInputRef}
								type='file'
								accept='.csv,.json,.txt,.pdf,.xml'
								multiple
								onChange={(e) => {
									const picked = Array.from(e.target.files).slice(0, 3);
									setFiles((prev) => [...prev, ...picked].slice(0, 3));
									e.target.value = '';
								}}
								style={{ display: 'none' }}
							/>
							{files.length > 0 ? (
								<div className='dropzone-filelist'>
									{files.map((f, i) => (
										<span key={i} className='dropzone-filename'>
											<span className='dropzone-icon'>📄</span>
											{f.name}
											<button
												className='dropzone-clear'
												onClick={(ev) => {
													ev.stopPropagation();
													setFiles((prev) => prev.filter((_, j) => j !== i));
												}}>
												×
											</button>
										</span>
									))}
									{files.length < 3 && (
										<span className='dropzone-add'>+ add more</span>
									)}
								</div>
							) : (
								<span className='dropzone-hint'>
									Drop files here or <u>click to upload</u>
									<span className='dropzone-types'>CSV · JSON · TXT · PDF · XML · up to 3 files</span>
								</span>
							)}
						</div>
					</div>

					<FilePreviewPanel previews={filePreviews} files={files} />

					<div className='action-row'>
						<button
							className='btn btn-primary'
							onClick={handleAnalyze}
							disabled={!context.trim() || status === 'running'}>
							{status === 'running' ? (
								<>
									<span className='btn-spinner' /> Running…
								</>
							) : (
								'Run Analysis'
							)}
						</button>
						{status === 'running' && (
							<button
								className='btn btn-ghost'
								onClick={() => {
									abortRef.current?.abort();
									setStatus('idle');
								}}>
								Stop
							</button>
						)}
						{(status === 'done' || status === 'error') && (
							<>
								<button
									className='btn btn-ghost'
									onClick={() => handleAnalyze()}
									title='Re-run with the same context and files'>
									↺ Rerun
								</button>
								<button
									className='btn btn-ghost'
									onClick={handleReset}>
									Reset
								</button>
							</>
						)}
						<span className='kbd-hint'>Ctrl+Enter</span>
					</div>
				</section>

				<section className='log-panel'>
					<PipelineDiagram
						logs={logs}
						status={status}
					/>

					<div className='tab-bar'>
						<div className='tab-bar-left'>
							<div className='log-dots'>
								<div className='log-dot log-dot-r' />
								<div className='log-dot log-dot-y' />
								<div className='log-dot log-dot-g' />
							</div>
							<button
								className={`tab-btn${activeTab === 'logs' ? ' tab-btn--active' : ''}`}
								onClick={() => setActiveTab('logs')}>
								Logs{' '}
								{logs.length > 0 && (
									<span className='tab-count'>{logs.length}</span>
								)}
							</button>
							<button
								className={`tab-btn${activeTab === 'result' ? ' tab-btn--active' : ''}${parsed ? ' tab-btn--has-result' : ''}`}
								onClick={() => setActiveTab('result')}
								disabled={!parsed}>
								Result{' '}
								{parsed && (
									<span className='tab-count tab-count--result'>✓</span>
								)}
							</button>
							{hasArtifacts && (
								<button
									className={`tab-btn${activeTab === 'artifacts' ? ' tab-btn--active' : ''} tab-btn--chart`}
									onClick={() => setActiveTab('artifacts')}>
									Artifacts{' '}
									<span className='tab-count tab-count--chart'>
										{parsed.data.output_type === 'code' ? '{ }' :
										 parsed.data.output_type === 'metrics' ? '◈' :
										 parsed.data.output_type === 'table' ? '⊞' :
										 parsed.data.output_type === 'comparison' ? '⇌' :
										 parsed.data.output_type === 'heatmap' ? '▦' : '📊'}
									</span>
								</button>
							)}
						</div>
						{status !== 'idle' && (
							<span className={`log-badge log-badge--${status}`}>
								{status === 'running' && <span className='badge-pulse' />}
								{status === 'running'
									? 'Live'
									: status === 'done'
										? 'Complete'
										: 'Error'}
							</span>
						)}
					</div>

					{activeTab === 'logs' && (
						<>
							<div
								className='log-terminal'
								role='log'
								aria-live='polite'>
								{logs.length === 0 && status === 'idle' && (
									<div className='log-empty'>
										<span className='log-empty-icon'>⬡</span>
										<span>Agent output will stream here in real-time.</span>
									</div>
								)}
								{logs.length === 0 && status === 'running' && (
									<div className='log-empty log-empty--active'>
										<span>Initializing agent pipeline</span>
									</div>
								)}
								{logs.map((line, i) => {
									const cls = getLogClass(line);
									if (cls === 'log-agent-header') {
										const agentIdx = detectAgentIndex(line);
										if (agentIdx !== -1) {
											const node = PIPELINE_NODES[agentIdx];
											return (
												<div
													key={i}
													className='log-step-card'
													style={{ '--nc': node.color }}>
													<span className='log-step-card-icon'>
														{node.icon}
													</span>
													<span className='log-step-card-name'>
														{node.label}
													</span>
												</div>
											);
										}
									}
									return (
										<div
											key={i}
											className={`log-line ${cls}`}>
											<span className='log-gutter'>
												{String(i + 1).padStart(3, ' ')}
											</span>
											<span className='log-text'>{line}</span>
										</div>
									);
								})}
								<div ref={logEndRef} />
							</div>
							{logs.length > 0 && (
								<div className='log-footer'>
									<span>{logs.length} lines</span>
									<button
										className='log-copy'
										onClick={() =>
											navigator.clipboard.writeText(logs.join('\n'))
										}>
										Copy all
									</button>
								</div>
							)}
						</>
					)}

					{activeTab === 'result' && (
						<div className='result-panel'>
							{!parsed ? (
								<div className='log-empty'>
									<span className='log-empty-icon'>⬡</span>
									<span>Result will appear once analysis completes.</span>
								</div>
							) : (
								<>
									<div className='result-type-bar'>
										<span className='result-type-badge'>
											{parsed.type === 'structured'
												? parsed.data.output_type.toUpperCase()
												: parsed.type.toUpperCase()}
										</span>
										{parsed.type === 'structured' && parsed.data.chart_type && (
											<span className='result-chart-badge'>
												{parsed.data.chart_type} chart
											</span>
										)}
										{parsed.type === 'structured' && parsed.data.quality_score != null && (
											<span
												className='quality-badge'
												style={{
													color: parsed.data.quality_score >= 8 ? '#34d399'
														: parsed.data.quality_score >= 5 ? '#fbbf24'
														: '#f87171',
												}}>
												Quality {parsed.data.quality_score}/10
											</span>
										)}
										{parsed.type === 'structured' && parsed.data.quality_verdict && (
											<span className='quality-verdict'>{parsed.data.quality_verdict}</span>
										)}
									</div>
									<div className='result-body'>
										<ResultView parsed={parsed} />
									</div>
									<div className='log-footer'>
										<span>{result.length} chars</span>
										<div style={{ display: 'flex', gap: '6px' }}>
											<button
												className='btn-share'
												onClick={shareResult}>
												{shareCopied ? '✓ Copied!' : '⎋ Share'}
											</button>
											<button
												className='btn-download'
												onClick={() => setShowDownload(true)}>
												⬇ Download
											</button>
										</div>
									</div>
								</>
							)}
						</div>
					)}

					{activeTab === 'artifacts' && hasArtifacts && (
						<ArtifactsPanel
							parsed={parsed}
							onDownload={() => setShowDownload(true)}
						/>
					)}
				</section>
			</main>

			{showDownload && parsed && (
				<DownloadPopup
					parsed={parsed}
					onClose={() => setShowDownload(false)}
				/>
			)}
			{showHistory && (
				<HistoryDrawer
					history={history}
					onSelect={handleHistorySelect}
					onClose={() => setShowHistory(false)}
				/>
			)}
		</div>
	);
}
