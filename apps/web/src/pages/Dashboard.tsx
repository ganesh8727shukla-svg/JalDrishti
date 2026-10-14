import { useEffect, useState } from 'react';
import ApiStatus from '../components/ApiStatus';
import AnalysisWorkflow from '../components/AnalysisWorkflow';
import MapView from '../components/MapView';
import { getHealth, getLatestCaseStudy } from '../services/api';
import type { ApiState, CaseStudyResponse } from '../types/api';

export default function Dashboard() {
  const [apiState, setApiState] = useState<ApiState>('checking');
  const [caseStudy, setCaseStudy] = useState<CaseStudyResponse | null>(null);
  const [caseStudyError, setCaseStudyError] = useState('');

  useEffect(() => {
    let active = true;
    getHealth()
      .then((health) => {
        if (active) setApiState(health.status === 'ok' ? 'online' : 'offline');
      })
      .catch(() => {
        if (active) setApiState('offline');
      });

    getLatestCaseStudy()
      .then((result) => {
        if (active) setCaseStudy(result);
      })
      .catch((error: unknown) => {
        if (active) {
          setCaseStudyError(error instanceof Error ? error.message : 'Case study is unavailable');
        }
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
          <span className="phase-tag">REAL DATA · MODEL PENDING</span>
        </div>
      </header>

      <section className="workspace">
        <aside className="sidebar" aria-label="Analysis setup">
          <div className="sidebar__intro">
            <span className="section-kicker">WORKSPACE</span>
            <h1>Flood overview</h1>
            <p>Search a chosen flood area and date, then review each data stage before it runs.</p>
          </div>

          <div className="data-status-card">
            <span className="section-kicker">DATA STATUS</span>
            <div className="data-status-row">
              <span className="status-indicator status-indicator--muted" aria-hidden="true" />
              <div>
                <strong>Sentinel-1 pair</strong>
                <span>{caseStudy ? 'SNAP processed · common grid validated' : caseStudyError || 'Loading real case study…'}</span>
              </div>
              <span className="status-trailing">{caseStudy ? '✓' : '…'}</span>
            </div>
            <div className="data-status-row">
              <span className="status-indicator status-indicator--muted" aria-hidden="true" />
              <div>
                <strong>Flood prediction</strong>
                <span>{caseStudy?.model.evaluation_status === 'completed_pending_review' ? 'Research evaluation complete · review required' : caseStudy ? 'No deployment-approved model' : 'No prediction loaded'}</span>
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
            <span className="map-panel__meta"><span className="legend-dot" /> Basemap · no flood overlay</span>
          </div>
          <MapView bbox={caseStudy?.event.aoi_bbox_wgs84} />
          <div className="map-footer">
            <span>Map tiles © OpenStreetMap contributors.</span>
            <span>Source imagery previews below are real; no flood mask is available yet.</span>
          </div>

          <AnalysisWorkflow initialBbox={caseStudy?.event.aoi_bbox_wgs84} initialEventDate={caseStudy?.event.event_date} />

          <section className="case-study" aria-label="Real satellite observations and model status">
            <div className="case-study__header">
              <div>
                <span className="section-kicker">SELECTED CASE STUDY</span>
                <h2>{caseStudy ? `Event ${caseStudy.event.event_date}` : 'Real-data outputs'}</h2>
              </div>
              {caseStudy && <span className="case-study__badge">{caseStudy.analysis_status.replaceAll('_', ' ')}</span>}
            </div>

            {caseStudy ? (
              <>
                <div className="pair-meta">
                  <span><b>Pre-event</b> {caseStudy.source.pre_acquisition_start.slice(0, 10)}</span>
                  <span><b>Post-event</b> {caseStudy.source.post_acquisition_start.slice(0, 10)}</span>
                  <span><b>Orbit</b> {caseStudy.source.orbit_direction}, relative {caseStudy.source.relative_orbit}</span>
                  <span><b>Polarization</b> {caseStudy.source.polarization.join(' / ')}</span>
                  <span><b>Grid</b> {caseStudy.rasters.pre_event.crs}, 10 m</span>
                </div>
                <div className="quicklook-grid">
                  <figure className="quicklook-card">
                    <img src={caseStudy.rasters.pre_event.quicklook_url} alt="Pre-event Sentinel-1 VV backscatter display quicklook" />
                    <figcaption><strong>Pre-event SAR</strong><span>{caseStudy.source.pre_scene_id}</span></figcaption>
                  </figure>
                  <figure className="quicklook-card">
                    <img src={caseStudy.rasters.post_event.quicklook_url} alt="Post-event Sentinel-1 VV backscatter display quicklook" />
                    <figcaption><strong>Post-event SAR</strong><span>{caseStudy.source.post_scene_id}</span></figcaption>
                  </figure>
                </div>
                <p className="quicklook-note">VV backscatter in dB with a shared percentile display stretch. These are display previews of real SNAP-processed data, not a flood classification.</p>

                <div className="result-status-grid">
                  <article className="result-status-card result-status-card--pending">
                    <span className="section-kicker">FLOOD MODEL</span>
                    <strong>{caseStudy.model.evaluation_status === 'completed_pending_review' ? 'Evaluation pending review' : 'Prediction pending'}</strong>
                    <p>{caseStudy.model.evaluation_status === 'completed_pending_review' ? 'The exploratory checkpoint has not been approved for Nepal inference; the SAR pair also remains outside the model-ready input gate.' : 'No deployment-approved checkpoint is available for this event.'}</p>
                    <dl><div><dt>Flooded area</dt><dd>Not calculated</dd></div><div><dt>Mask / boundaries</dt><dd>Unavailable</dd></div></dl>
                  </article>
                  <article className="result-status-card">
                    <span className="section-kicker">PRE-EVENT OSM INVENTORY</span>
                    {caseStudy.infrastructure ? (
                      <>
                        <strong>Provider dated {caseStudy.infrastructure.provider_snapshot_date}</strong>
                        <div className="infrastructure-counts">
                          {Object.entries(caseStudy.infrastructure.feature_counts_by_category).map(([category, count]) => (
                            <span key={category}><b>{count.toLocaleString()}</b> {category}</span>
                          ))}
                        </div>
                        <p>Inventory only; no flood intersection or road-access result is available.</p>
                      </>
                    ) : <p>No matching prepared historical inventory was found for this pair.</p>}
                  </article>
                </div>
                <div className="provenance-note">
                  <span>{caseStudy.attribution.join(' · ')}</span>
                  {caseStudy.warnings.map((warning) => <p key={warning}>{warning}</p>)}
                </div>
              </>
            ) : (
              <div className="case-study__empty" role="status">
                {caseStudyError ? `Real case-study data is unavailable: ${caseStudyError}` : 'Loading the latest completed real Sentinel-1 preprocessing job…'}
              </div>
            )}
          </section>
        </section>
      </section>
    </main>
  );
}
