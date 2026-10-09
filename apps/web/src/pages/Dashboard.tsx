import { useEffect, useState } from 'react';
import ApiStatus from '../components/ApiStatus';
import MapView from '../components/MapView';
import { getHealth } from '../services/api';
import type { ApiState } from '../types/api';

export default function Dashboard() {
  const [apiState, setApiState] = useState<ApiState>('checking');
  const [area, setArea] = useState('');
  const [eventDate, setEventDate] = useState('');

  useEffect(() => {
    let active = true;
    getHealth()
      .then((health) => {
        if (active) setApiState(health.status === 'ok' ? 'online' : 'offline');
      })
      .catch(() => {
        if (active) setApiState('offline');
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="JalDrishti home">
          <span className="brand__mark" aria-hidden="true">J</span>
          <span className="brand__name">JalDrishti</span>
        </a>
        <div className="topbar__right">
          <span className="topbar__workspace">Flood intelligence workspace</span>
          <ApiStatus state={apiState} />
          <span className="phase-tag">PHASE 1</span>
        </div>
      </header>

      <section className="workspace">
        <aside className="sidebar" aria-label="Analysis setup">
          <div className="sidebar__intro">
            <span className="section-kicker">WORKSPACE</span>
            <h1>Flood overview</h1>
            <p>Set an area and event date to prepare an analysis.</p>
          </div>

          <form className="setup-card" onSubmit={(event) => event.preventDefault()}>
            <div className="card-heading">
              <span className="card-heading__icon" aria-hidden="true">⌖</span>
              <div>
                <h2>Analysis setup</h2>
                <p>Define the event area</p>
              </div>
            </div>

            <label className="field-label" htmlFor="area">Area of interest</label>
            <input
              id="area"
              name="area"
              type="text"
              value={area}
              onChange={(event) => setArea(event.target.value)}
              placeholder="Enter a place or region"
              autoComplete="off"
            />

            <label className="field-label" htmlFor="event-date">Flood event date</label>
            <input
              id="event-date"
              name="event-date"
              type="date"
              value={eventDate}
              onChange={(event) => setEventDate(event.target.value)}
            />

            <button className="start-button" type="button" disabled title="Satellite discovery is not implemented yet">
              <span aria-hidden="true">＋</span> Start analysis
            </button>
            <p className="form-note">Satellite discovery is the next milestone. No analysis runs yet.</p>
          </form>

          <div className="data-status-card">
            <span className="section-kicker">DATA STATUS</span>
            <div className="data-status-row">
              <span className="status-indicator status-indicator--muted" aria-hidden="true" />
              <div>
                <strong>Sentinel-1</strong>
                <span>Not searched</span>
              </div>
              <span className="status-trailing">—</span>
            </div>
            <div className="data-status-row">
              <span className="status-indicator status-indicator--muted" aria-hidden="true" />
              <div>
                <strong>Flood layer</strong>
                <span>No analysis</span>
              </div>
              <span className="status-trailing">—</span>
            </div>
          </div>

          <div className="sidebar__footer">
            <span className="footer-mark" aria-hidden="true">◎</span>
            <span>Built for evidence-led response planning</span>
          </div>
        </aside>

        <section className="map-panel" aria-label="Map and analysis status">
          <div className="map-panel__header">
            <div>
              <span className="section-kicker">GEOSPATIAL VIEW</span>
              <h2>Area overview</h2>
            </div>
            <span className="map-panel__meta"><span className="legend-dot" /> No active analysis</span>
          </div>
          <MapView />
          <div className="map-footer">
            <span>Map tiles © OpenStreetMap contributors.</span>
            <span>Satellite layers will appear here after a successful analysis.</span>
          </div>
        </section>
      </section>
    </main>
  );
}
