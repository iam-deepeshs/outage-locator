import { useState, useEffect, useCallback } from 'react'
import './App.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || `Request failed: ${res.status}`)
  }
  return data
}

const STATUS_LABELS = {
  detected: 'Detected',
  acknowledged: 'Acknowledged',
  crew_assigned: 'Crew assigned',
  resolved: 'Resolved (unverified)',
  verified: 'Verified',
  closed: 'Closed',
}

const NEXT_ACTION = {
  detected: { action: 'acknowledge', label: 'Acknowledge' },
  acknowledged: { action: 'assign-crew', label: 'Assign crew' },
  crew_assigned: { action: 'mark-resolved', label: 'Mark resolved' },
  resolved: { action: 'verify', label: 'Verify now' },
  verified: { action: 'close', label: 'Close ticket' },
  closed: null,
}

function confidenceBand(c) {
  if (c >= 0.8) return { label: 'High', color: 'var(--live)' }
  if (c >= 0.5) return { label: 'Medium', color: 'var(--amber)' }
  return { label: 'Low', color: 'var(--fault)' }
}

function IncidentRow({ incident, selected, onSelect, onAction, actionError }) {
  const conf = confidenceBand(incident.confidence)
  const next = NEXT_ACTION[incident.status]
  return (
    <div
      className={`incident-row ${selected ? 'selected' : ''} status-${incident.status}`}
      onClick={() => onSelect(incident.id)}
    >
      <div className="incident-row-top">
        <span className={`type-badge type-${incident.type}`}>{incident.type}</span>
        <span className="mono pincode">{incident.pincode || '—'}</span>
        <span className="status-pill">{STATUS_LABELS[incident.status]}</span>
      </div>
      <div className="incident-row-mid">
        <span className="mono">{incident.dt_id || incident.feeder_id || '—'}</span>
        <span className="pole-count">{incident.affected_pole_count} poles affected</span>
      </div>
      <div className="incident-row-bottom">
        <span className="confidence-dot" style={{ background: conf.color }} />
        <span className="confidence-label">{conf.label} confidence ({(incident.confidence * 100).toFixed(0)}%)</span>
        {next && (
          <button
            className="action-btn"
            onClick={(e) => { e.stopPropagation(); onAction(incident.id, next.action) }}
          >
            {next.label}
          </button>
        )}
      </div>
      {actionError && <div className="action-error">{actionError}</div>}
    </div>
  )
}

function SchematicMap({ poles, incidents, selectedIncidentId }) {
  if (!poles.length) return <div className="map-empty">No pole data loaded</div>

  const lats = poles.map(p => p.lat)
  const lons = poles.map(p => p.lon)
  const minLat = Math.min(...lats), maxLat = Math.max(...lats)
  const minLon = Math.min(...lons), maxLon = Math.max(...lons)
  const pad = 20
  const W = 600, H = 500

  const x = (lon) => pad + ((lon - minLon) / (maxLon - minLon || 1)) * (W - 2 * pad)
  const y = (lat) => H - pad - ((lat - minLat) / (maxLat - minLat || 1)) * (H - 2 * pad)

  const selected = incidents.find(i => i.id === selectedIncidentId)
  const highlightDt = selected?.dt_id
  const highlightFeeder = selected?.feeder_id

  const isHighlighted = (p) =>
    (highlightDt && p.dt_id === highlightDt) || (highlightFeeder && p.feeder_id === highlightFeeder)

  const normalPoles = poles.filter(p => !isHighlighted(p))
  const highlightedPoles = poles.filter(p => isHighlighted(p))

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="schematic-map">
      {normalPoles.map(p => (
        <circle key={p.pole_id} cx={x(p.lon)} cy={y(p.lat)} r={1.8} className="pole-dot" />
      ))}
      {highlightedPoles.map(p => (
        <circle key={p.pole_id} cx={x(p.lon)} cy={y(p.lat)} r={3.5} className="pole-dot highlighted" />
      ))}
    </svg>
  )
}

function SimulatorPanel({ onRefresh }) {
  const [dtId, setDtId] = useState('D-0001')
  const [feederId, setFeederId] = useState('F-01-01')
  const [busy, setBusy] = useState(false)
  const [log, setLog] = useState([])
  const [expanded, setExpanded] = useState(false)

  const run = async (label, fn) => {
    setBusy(true)
    try {
      const result = await fn()
      setLog(l => [`${label}: ${JSON.stringify(result).slice(0, 120)}`, ...l].slice(0, 6))
      onRefresh()
    } catch (e) {
      setLog(l => [`${label} FAILED: ${e.message}`, ...l].slice(0, 6))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`sim-panel ${expanded ? 'expanded' : ''}`}>
      <button className="sim-toggle" onClick={() => setExpanded(e => !e)}>
        {expanded ? '▾' : '▸'} Simulator controls (test/demo only)
      </button>
      {expanded && (
        <div className="sim-body">
          <div className="sim-row">
            <button disabled={busy} onClick={() => run('tick', () => api('/simulator/tick?minutes=20', { method: 'POST' }))}>
              Advance time (+20 min)
            </button>
            <button disabled={busy} onClick={() => run('run localization', () => api('/debug/localization/run', { method: 'POST' }))}>
              Run localization
            </button>
            <button disabled={busy} onClick={() => run('reset', () => api('/simulator/reset', { method: 'POST' }))}>
              Reset simulator
            </button>
          </div>
          <div className="sim-row">
            <input value={dtId} onChange={e => setDtId(e.target.value)} placeholder="DT id, e.g. D-0001" />
            <button disabled={busy} onClick={() => run('span fault', () => api(`/simulator/fault/span?dt_id=${dtId}`, { method: 'POST' }))}>
              Inject span fault
            </button>
            <button disabled={busy} onClick={() => run('dt fault', () => api(`/simulator/fault/dt?dt_id=${dtId}`, { method: 'POST' }))}>
              Inject DT fault
            </button>
          </div>
          <div className="sim-row">
            <input value={feederId} onChange={e => setFeederId(e.target.value)} placeholder="Feeder id, e.g. F-01-01" />
            <button disabled={busy} onClick={() => run('feeder fault', () => api(`/simulator/fault/feeder?feeder_id=${feederId}`, { method: 'POST' }))}>
              Inject feeder fault
            </button>
          </div>
          <div className="sim-log">
            {log.map((l, i) => <div key={i} className="sim-log-line mono">{l}</div>)}
          </div>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [incidents, setIncidents] = useState([])
  const [poles, setPoles] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [actionErrors, setActionErrors] = useState({})
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const [inc] = await Promise.all([api('/tickets')])
      setIncidents(inc)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    // Also load pole positions once for the schematic map
    api('/network/poles').then(setPoles).catch(() => {})
    const interval = setInterval(refresh, 5000)
    return () => clearInterval(interval)
  }, [refresh])

  const handleAction = async (id, action) => {
    setActionErrors(e => ({ ...e, [id]: null }))
    try {
      await api(`/tickets/${id}/${action}`, { method: 'POST' })
      await refresh()
    } catch (e) {
      setActionErrors(errs => ({ ...errs, [id]: e.message }))
    }
  }

  const openCount = incidents.filter(i => i.status !== 'closed').length
  const criticalCount = incidents.filter(i => i.status !== 'closed' && i.confidence >= 0.8).length

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-title">
          <span className="grid-icon">⚡</span>
          <span>KSPDB Outage Control</span>
        </div>
        <div className="header-stats">
          <span className="stat"><strong>{openCount}</strong> open</span>
          <span className="stat critical"><strong>{criticalCount}</strong> high confidence</span>
        </div>
      </header>

      <div className="app-body">
        <aside className="incident-panel">
          <div className="panel-header">Incidents</div>
          {loading && <div className="empty-state">Loading…</div>}
          {!loading && incidents.length === 0 && (
            <div className="empty-state">No active incidents. Network is healthy.</div>
          )}
          <div className="incident-list">
            {incidents.map(inc => (
              <IncidentRow
                key={inc.id}
                incident={inc}
                selected={inc.id === selectedId}
                onSelect={setSelectedId}
                onAction={handleAction}
                actionError={actionErrors[inc.id]}
              />
            ))}
          </div>
        </aside>

        <main className="map-panel">
          <div className="panel-header">Network schematic</div>
          <SchematicMap poles={poles} incidents={incidents} selectedIncidentId={selectedId} />
          <div className="map-legend">
            <span><span className="legend-dot" style={{ background: 'var(--muted)' }} /> Pole</span>
            <span><span className="legend-dot highlighted" /> Poles in selected incident</span>
          </div>
        </main>
      </div>

      <footer>
        <SimulatorPanel onRefresh={refresh} />
      </footer>
    </div>
  )
}
