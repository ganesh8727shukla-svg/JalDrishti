import { useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  getFloodAnalysis,
  getProductDownload,
  getSarPreprocessing,
  previewProductPair,
  searchObservationPairs,
  startFloodAnalysis,
  startProductDownload,
  startSarPreprocessing,
} from '../services/api';
import type {
  FloodAnalysisResponse,
  ObservationMetadata,
  ObservationPairCandidate,
  ObservationPairSearchResponse,
  ProductDownloadResponse,
  ProductPairDownloadPreview,
  SARPreprocessingResponse,
} from '../types/api';

type Props = { initialBbox?: number[]; initialEventDate?: string };

function addDays(dateValue: string, delta: number): string {
  const date = new Date(`${dateValue}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return 'Unknown';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit >= 3 ? 2 : 1)} ${units[unit]}`;
}

function parseBbox(raw: string): number[] {
  const values = raw.split(',').map((value) => Number(value.trim()));
  if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) {
    throw new Error('Enter four numbers in west, south, east, north order.');
  }
  const [west, south, east, north] = values;
  if (west! < -180 || east! > 180 || south! < -90 || north! > 90 || west! >= east! || south! >= north!) {
    throw new Error('The bounding box must be valid WGS84 coordinates with west < east and south < north.');
  }
  return values;
}

function utmForBbox(bbox: number[]): string {
  const centerLon = (bbox[0]! + bbox[2]!) / 2;
  const centerLat = (bbox[1]! + bbox[3]!) / 2;
  if (centerLat < -80 || centerLat > 84) return '';
  const zone = Math.min(60, Math.max(1, Math.floor((centerLon + 180) / 6) + 1));
  const epsg = (centerLat >= 0 ? 32600 : 32700) + zone;
  return `EPSG:${epsg}`;
}

function pairKey(pair: ObservationPairCandidate): string {
  return `${pair.pre_scene_id}::${pair.post_scene_id}`;
}

function observationDate(observations: ObservationMetadata[], sceneId: string): string {
  return observations.find((item) => item.scene_id === sceneId)?.acquisition_start?.slice(0, 10) ?? 'date unavailable';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'The request failed. Check the API and try again.';
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForDownload(
  id: string,
  onUpdate: (value: ProductDownloadResponse) => void,
): Promise<ProductDownloadResponse> {
  for (;;) {
    const current = await getProductDownload(id);
    onUpdate(current);
    if (current.status === 'geospatial_inspected' || current.status === 'failed' || current.status === 'validation_failed') return current;
    await sleep(4000);
  }
}

async function waitForPreprocessing(
  id: string,
  onUpdate: (value: SARPreprocessingResponse) => void,
): Promise<SARPreprocessingResponse> {
  for (;;) {
    const current = await getSarPreprocessing(id);
    onUpdate(current);
    if (current.status === 'completed' || current.status === 'failed') return current;
    await sleep(5000);
  }
}

async function waitForAnalysis(id: string, onUpdate: (value: FloodAnalysisResponse) => void): Promise<FloodAnalysisResponse> {
  for (;;) {
    const current = await getFloodAnalysis(id);
    onUpdate(current);
    if (current.status === 'blocked' || current.status === 'pending_independent_validation' || current.status === 'completed' || current.status === 'failed') return current;
    await sleep(3000);
  }
}

export default function AnalysisWorkflow({ initialBbox, initialEventDate }: Props) {
  const [bboxText, setBboxText] = useState(initialBbox?.join(', ') ?? '');
  const [eventDate, setEventDate] = useState(initialEventDate ?? '');
  const [windowDays, setWindowDays] = useState(30);
  const [searchResult, setSearchResult] = useState<ObservationPairSearchResponse | null>(null);
  const [selectedKey, setSelectedKey] = useState('');
  const [showAllPairs, setShowAllPairs] = useState(false);
  const [preview, setPreview] = useState<ProductPairDownloadPreview | null>(null);
  const [downloads, setDownloads] = useState<Partial<Record<'pre_event' | 'post_event', ProductDownloadResponse>>>({});
  const [preprocessing, setPreprocessing] = useState<SARPreprocessingResponse | null>(null);
  const [analysis, setAnalysis] = useState<FloodAnalysisResponse | null>(null);
  const [targetCrs, setTargetCrs] = useState(initialBbox ? utmForBbox(initialBbox) : '');
  const [pixelSpacing, setPixelSpacing] = useState(10);
  const [allowAuxiliary, setAllowAuxiliary] = useState(false);
  const [loadedInitialCase, setLoadedInitialCase] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const selectedPair = useMemo(
    () => searchResult?.pairs.find((pair) => pairKey(pair) === selectedKey) ?? null,
    [searchResult, selectedKey],
  );
  const observations = searchResult?.observations ?? [];
  const downloadReady = downloads.pre_event?.status === 'geospatial_inspected' && downloads.post_event?.status === 'geospatial_inspected';
  const diskEnough = Boolean(preview && preview.available_disk_bytes >= preview.total_size_bytes);

  useEffect(() => {
    if (loadedInitialCase || !initialBbox || !initialEventDate) return;
    setBboxText(initialBbox.join(', '));
    setEventDate(initialEventDate);
    setTargetCrs(utmForBbox(initialBbox));
    setLoadedInitialCase(true);
  }, [initialBbox, initialEventDate, loadedInitialCase]);

  function invalidateSelection() {
    setSearchResult(null);
    setSelectedKey('');
    setPreview(null);
    setDownloads({});
    setPreprocessing(null);
    setAnalysis(null);
    setShowAllPairs(false);
    setError('');
    setNotice('');
  }

  function selectPair(pair: ObservationPairCandidate) {
    setSelectedKey(pairKey(pair));
    setPreview(null);
    setDownloads({});
    setPreprocessing(null);
    setAnalysis(null);
    setShowAllPairs(false);
    setShowAllPairs(false);
    setError('');
    setNotice('');
    const bbox = parseBbox(bboxText);
    setTargetCrs(utmForBbox(bbox));
  }

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setNotice('');
    setPreview(null);
    setDownloads({});
    setPreprocessing(null);
    setAnalysis(null);
    setBusy('search');
    try {
      const bbox = parseBbox(bboxText);
      if (!eventDate) throw new Error('Choose the flood event date.');
      if (!Number.isInteger(windowDays) || windowDays < 1 || windowDays > 365) throw new Error('Search window must be from 1 to 365 days.');
      const result = await searchObservationPairs({
        bbox: bbox.join(','),
        event_date: eventDate,
        pre_event_start: addDays(eventDate, -windowDays),
        pre_event_end: addDays(eventDate, -1),
        post_event_start: addDays(eventDate, 1),
        post_event_end: addDays(eventDate, windowDays),
        limit_per_period: 10,
      });
      setSearchResult(result);
      const best = result.pairs.find((pair) => pair.verification_status === 'compatible');
      if (best) {
        setSelectedKey(pairKey(best));
        setTargetCrs(utmForBbox(bbox));
      } else {
        setSelectedKey('');
        setNotice('No pair passed the compatibility checks. Review the reasons below or adjust the AOI, date, or search window.');
      }
    } catch (requestError) {
      setSearchResult(null);
      setSelectedKey('');
      setError(errorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  async function handlePreview() {
    if (!selectedPair) return;
    setError('');
    setNotice('');
    setBusy('preview');
    try {
      const result = await previewProductPair({
        pre_scene_id: selectedPair.pre_scene_id,
        post_scene_id: selectedPair.post_scene_id,
        event_date: eventDate,
        bbox: parseBbox(bboxText),
      });
      setPreview(result);
      if (result.available_disk_bytes < result.total_size_bytes) {
        setNotice('The current free disk space is below the combined raw product size. Downloads are disabled until storage is available.');
      }
    } catch (requestError) {
      setPreview(null);
      setError(errorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  async function handleDownload() {
    if (!selectedPair || !preview || !diskEnough) return;
    setError('');
    setNotice('');
    const selection = {
      pre_scene_id: selectedPair.pre_scene_id,
      post_scene_id: selectedPair.post_scene_id,
      event_date: eventDate,
      bbox: parseBbox(bboxText),
      asset_key: 'Product' as const,
    };
    try {
      setBusy('pre_download');
      setNotice('Downloading and validating the selected pre-event product. The post-event download will start after it finishes.');
      const preJob = await startProductDownload({ ...selection, role: 'pre_event' });
      setDownloads((current) => ({ ...current, pre_event: preJob }));
      const preFinal = preJob.status === 'geospatial_inspected' || preJob.status === 'failed' || preJob.status === 'validation_failed'
        ? preJob : await waitForDownload(preJob.download_id, (value) => setDownloads((current) => ({ ...current, pre_event: value })));
      if (preFinal.status !== 'geospatial_inspected') throw new Error(preFinal.error?.message ?? 'The pre-event product did not pass archive and raster metadata validation.');

      setBusy('post_download');
      setNotice('Pre-event product validated. Downloading and validating the selected post-event product.');
      const postJob = await startProductDownload({ ...selection, role: 'post_event' });
      setDownloads((current) => ({ ...current, post_event: postJob }));
      const postFinal = postJob.status === 'geospatial_inspected' || postJob.status === 'failed' || postJob.status === 'validation_failed'
        ? postJob : await waitForDownload(postJob.download_id, (value) => setDownloads((current) => ({ ...current, post_event: value })));
      if (postFinal.status !== 'geospatial_inspected') throw new Error(postFinal.error?.message ?? 'The post-event product did not pass archive and raster metadata validation.');
      setNotice('Both source products passed archive integrity and raster metadata checks. They are not yet calibrated, terrain corrected, or model ready.');
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  async function handlePreprocess() {
    if (!downloadReady) return;
    setError('');
    setNotice('');
    setBusy('preprocessing');
    try {
      const bbox = parseBbox(bboxText);
      if (!targetCrs.trim()) throw new Error('Choose a projected target CRS in metres.');
      const started = await startSarPreprocessing({
        pre_download_id: downloads.pre_event!.download_id,
        post_download_id: downloads.post_event!.download_id,
        bbox,
        target_crs: targetCrs.trim(),
        pixel_spacing_m: pixelSpacing,
        dem_name: 'Copernicus 30m Global DEM',
        allow_snap_auxiliary_downloads: allowAuxiliary,
      });
      setPreprocessing(started);
      setNotice('SNAP preprocessing is running. Status updates will appear here.');
      const finalJob = started.status === 'completed' || started.status === 'failed'
        ? started : await waitForPreprocessing(started.job_id, setPreprocessing);
      if (finalJob.status === 'failed') throw new Error(finalJob.error?.message ?? 'SNAP processing failed; inspect the local processing manifest and server log.');
      setNotice(`SNAP processing completed. Model-input status: ${finalJob.model_input_status ?? 'not verified'}.`);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  async function handleAnalysis() {
    if (preprocessing?.status !== 'completed') return;
    setError('');
    setNotice('');
    setBusy('analysis');
    try {
      const started = await startFloodAnalysis(preprocessing.job_id);
      setAnalysis(started);
      const finalJob = started.status === 'blocked' || started.status === 'pending_independent_validation' || started.status === 'completed' || started.status === 'failed'
        ? started : await waitForAnalysis(started.analysis_id, setAnalysis);
      setAnalysis(finalJob);
      setNotice(finalJob.status === 'completed' ? 'Validated inference results are available below.' : 'No flood result was fabricated; the backend reports the current model and validation gate below.');
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy('');
    }
  }

  return (
    <section className="analysis-workflow" aria-label="Run a flood analysis">
      <div className="analysis-workflow__header">
        <div>
          <span className="section-kicker">SELECTED AREA WORKFLOW</span>
          <h2>Find observations and run analysis</h2>
        </div>
        <span className="analysis-workflow__tag">Each data transfer requires a size preview and a separate click</span>
      </div>

      <form className="workflow-form" onSubmit={handleSearch}>
        <label>
          <span>AOI bounding box (W, S, E, N)</span>
          <input value={bboxText} onChange={(event) => { setBboxText(event.target.value); invalidateSelection(); }} placeholder="84.04, 28.20, 84.15, 28.31" required />
        </label>
        <label>
          <span>Flood event date</span>
          <input type="date" value={eventDate} onChange={(event) => { setEventDate(event.target.value); invalidateSelection(); }} required />
        </label>
        <label>
          <span>Search days before and after</span>
          <input type="number" min={1} max={365} value={windowDays} onChange={(event) => { setWindowDays(Number(event.target.value)); invalidateSelection(); }} required />
        </label>
        <button className="workflow-button workflow-button--primary" type="submit" disabled={Boolean(busy)}>
          {busy === 'search' ? 'Searching CDSE…' : 'Find Sentinel-1 pairs'}
        </button>
      </form>
      <p className="workflow-hint">The search checks the date windows around the event and ranks pairs by orbit, polarization, product type, and AOI coverage. It does not download imagery.</p>

      {searchResult && (
        <div className="workflow-results">
          <h3>Candidate pairs <span>{searchResult.pairs.length} found</span></h3>
          {searchResult.pairs.length === 0 ? <p className="workflow-empty">No pair was returned for these criteria.</p> : (showAllPairs ? searchResult.pairs : searchResult.pairs.slice(0, 10)).map((pair) => {
            const compatible = pair.verification_status === 'compatible';
            return (
              <label className={`pair-option${pairKey(pair) === selectedKey ? ' pair-option--selected' : ''}${!compatible ? ' pair-option--disabled' : ''}`} key={pairKey(pair)}>
                <input type="radio" name="observation-pair" checked={pairKey(pair) === selectedKey} disabled={!compatible || Boolean(busy)} onChange={() => selectPair(pair)} />
                <span className="pair-option__content">
                  <span className="pair-option__title"><b>Rank {pair.rank}</b> · {pair.verification_status.replaceAll('_', ' ')}</span>
                  <span>Pre {observationDate(observations, pair.pre_scene_id)} → Post {observationDate(observations, pair.post_scene_id)} · {pair.elapsed_days === null ? 'interval unavailable' : `${pair.elapsed_days.toFixed(1)} days`}</span>
                  <span className="pair-option__ids">{pair.pre_scene_id}<br />{pair.post_scene_id}</span>
                  {pair.ranking_reasons.map((reason) => <span className="pair-option__reason" key={reason}>• {reason}</span>)}
                  {pair.warnings.map((warning) => <span className="pair-option__warning" key={warning}>Warning: {warning}</span>)}
                </span>
              </label>
            );
          })}
          {searchResult.pairs.length > 10 && <button className="workflow-button workflow-button--list" type="button" onClick={() => setShowAllPairs((visible) => !visible)}>{showAllPairs ? 'Show top 10 pairs' : `Show all ${searchResult.pairs.length} pairs`}</button>}
          {searchResult.warnings.map((warning) => <p className="workflow-hint" key={warning}>{warning}</p>)}
        </div>
      )}

      {selectedPair && (
        <div className="workflow-stage">
          <button className="workflow-button" type="button" onClick={handlePreview} disabled={Boolean(busy) || !selectedPair}>
            {busy === 'preview' ? 'Checking CDSE availability…' : 'Check availability and total size'}
          </button>
          {preview && (
            <div className="preview-card" role="status">
              <h3>Explicitly selected native product assets</h3>
              <div><span>Pre-event · {preview.pre_event.product_name}</span><b>{formatBytes(preview.pre_event.expected_size_bytes)}</b></div>
              <div><span>Post-event · {preview.post_event.product_name}</span><b>{formatBytes(preview.post_event.expected_size_bytes)}</b></div>
              <div className="preview-card__total"><span>Combined raw download</span><b>{formatBytes(preview.total_size_bytes)}</b></div>
              <div><span>Current free disk space</span><b>{formatBytes(preview.available_disk_bytes)}</b></div>
              {preview.warnings.map((warning) => <p key={warning}>{warning}</p>)}
              <button className="workflow-button workflow-button--primary" type="button" onClick={handleDownload} disabled={Boolean(busy) || !diskEnough}>
                {busy.includes('download') ? 'Downloading one product at a time…' : 'Download these two selected products'}
              </button>
            </div>
          )}
        </div>
      )}

      {(downloads.pre_event || downloads.post_event) && (
        <div className="workflow-job-list" aria-live="polite">
          {(['pre_event', 'post_event'] as const).map((role) => {
            const job = downloads[role];
            if (!job) return null;
            const percent = job.expected_size_bytes && job.expected_size_bytes > 0 ? Math.min(100, job.progress_bytes / job.expected_size_bytes * 100) : 0;
            return <div className="workflow-job" key={role}>
              <div><b>{role.replace('_', ' ')}</b><span>{job.status.replaceAll('_', ' ')} · {formatBytes(job.progress_bytes)} / {job.expected_size_bytes ? formatBytes(job.expected_size_bytes) : 'size pending'}</span></div>
              <progress max={100} value={percent} aria-label={`${role} download progress`} />
              {job.error && <p className="workflow-error">{job.error.message}</p>}
            </div>;
          })}
        </div>
      )}

      {downloadReady && (
        <div className="workflow-stage">
          <h3>Source files passed integrity and geospatial metadata inspection</h3>
          <p className="workflow-hint">This does not establish calibration or model readiness. The next explicit step runs the configured SNAP workflow.</p>
          <div className="workflow-config">
            <label><span>Projected target CRS</span><input value={targetCrs} onChange={(event) => setTargetCrs(event.target.value)} placeholder="EPSG:32645" /></label>
            <label><span>Pixel spacing (metres)</span><input type="number" min={1} max={1000} value={pixelSpacing} onChange={(event) => setPixelSpacing(Number(event.target.value))} /></label>
          </div>
          <label className="workflow-checkbox"><input type="checkbox" checked={allowAuxiliary} onChange={(event) => setAllowAuxiliary(event.target.checked)} /><span>Allow SNAP to download required orbit/DEM auxiliary files</span></label>
          <button className="workflow-button workflow-button--primary" type="button" disabled={Boolean(busy) || preprocessing?.status === 'running'} onClick={handlePreprocess}>
            {busy === 'preprocessing' ? 'SNAP processing…' : 'Start SNAP preprocessing'}
          </button>
          {preprocessing && <p className="workflow-job-summary">Preprocessing {preprocessing.status.replaceAll('_', ' ')} · {preprocessing.stage}{preprocessing.model_input_status ? ` · model input: ${preprocessing.model_input_status}` : ''}</p>}
        </div>
      )}

      {preprocessing?.status === 'completed' && (
        <div className="workflow-stage">
          <h3>Preprocessing completed</h3>
          <p className="workflow-hint">Run analysis to evaluate the model/input approval gates. A flood mask will appear only if actual inference succeeds and validation passes.</p>
          <button className="workflow-button workflow-button--primary" type="button" disabled={Boolean(busy)} onClick={handleAnalysis}>
            {busy === 'analysis' ? 'Checking inference eligibility…' : 'Run flood analysis'}
          </button>
        </div>
      )}

      {analysis && (
        <div className={`analysis-result analysis-result--${analysis.status}`} role="status">
          <h3>Analysis: {analysis.status.replaceAll('_', ' ')}</h3>
          <p>Stage: {analysis.stage}</p>
          {analysis.error && <p>{analysis.error.message}</p>}
          {analysis.status === 'completed' ? (
            <>
              <p>Flooded area: {analysis.results.flooded_area_km2 ?? 'Unavailable'} km² · CRS {analysis.results.crs ?? 'unavailable'}</p>
              {analysis.rasters.flood_mask?.url && <a href={analysis.rasters.flood_mask.url} target="_blank" rel="noreferrer">Open validated flood mask GeoTIFF</a>}
              {analysis.boundaries?.url && <a href={analysis.boundaries.url} target="_blank" rel="noreferrer">Open flood boundaries GeoJSON</a>}
            </>
          ) : <p>No mask, confidence value, flooded area, or impact total is presented as a result unless it was produced and passed the backend validation gates.</p>}
          {analysis.warnings.map((warning) => <p className="workflow-hint" key={warning}>{warning}</p>)}
        </div>
      )}

      {notice && <p className="workflow-notice" role="status">{notice}</p>}
      {error && <p className="workflow-error" role="alert">{error}</p>}
    </section>
  );
}
