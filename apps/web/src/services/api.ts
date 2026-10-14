import type {
  CaseStudyResponse,
  FloodAnalysisResponse,
  HealthResponse,
  ObservationPairSearchResponse,
  ProductDownloadResponse,
  ProductPairDownloadPreview,
  SARPreprocessingResponse,
} from '../types/api';

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
      const detail = payload.detail;
      if (typeof detail === 'object' && detail !== null && 'message' in detail && typeof detail.message === 'string') {
        message = detail.message;
      } else if (typeof detail === 'string') {
        message = detail;
      }
    }
    throw new Error(message);
  }
  if (payload === null || typeof payload !== 'object') throw new Error('API returned an invalid response');
  return payload as T;
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch('/api/v1/health');
  if (!response.ok) {
    throw new Error(`API health request failed (${response.status})`);
  }

  const payload: unknown = await response.json();
  if (
    typeof payload !== 'object' ||
    payload === null ||
    !('status' in payload) ||
    typeof payload.status !== 'string'
  ) {
    throw new Error('API returned an invalid health response');
  }

  return { status: payload.status };
}

export async function getLatestCaseStudy(): Promise<CaseStudyResponse> {
  const response = await fetch('/api/v1/case-study/latest');
  if (!response.ok) {
    throw new Error(`Case study request failed (${response.status})`);
  }

  const payload: unknown = await response.json();
  if (
    typeof payload !== 'object' || payload === null ||
    !('schema_version' in payload) || typeof payload.schema_version !== 'string' ||
    !('analysis_status' in payload) || typeof payload.analysis_status !== 'string' ||
    !('rasters' in payload) || typeof payload.rasters !== 'object' || payload.rasters === null ||
    !('model' in payload) || typeof payload.model !== 'object' || payload.model === null
  ) {
    throw new Error('API returned an invalid case-study response');
  }

  return payload as CaseStudyResponse;
}

export async function startFloodAnalysis(preprocessingJobId: string): Promise<FloodAnalysisResponse> {
  const payload = await requestJson<FloodAnalysisResponse>('/api/v1/analyses', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preprocessing_job_id: preprocessingJobId }),
  });
  if (!payload.analysis_id || !payload.status || !payload.preprocessing_job_id) {
    throw new Error('API returned an invalid flood analysis response');
  }
  return payload;
}

export async function getFloodAnalysis(analysisId: string): Promise<FloodAnalysisResponse> {
  const payload = await requestJson<FloodAnalysisResponse>(`/api/v1/analyses/${encodeURIComponent(analysisId)}`);
  if (!payload.analysis_id || !payload.status) {
    throw new Error('API returned an invalid flood analysis status');
  }
  return payload;
}

export type PairSearchRequest = {
  bbox: string;
  event_date: string;
  pre_event_start: string;
  pre_event_end: string;
  post_event_start: string;
  post_event_end: string;
  limit_per_period: number;
};

export async function searchObservationPairs(query: PairSearchRequest): Promise<ObservationPairSearchResponse> {
  const params = new URLSearchParams(query as unknown as Record<string, string>);
  return requestJson(`/api/v1/observations/pairs?${params.toString()}`);
}

export type PairSelection = {
  pre_scene_id: string;
  post_scene_id: string;
  event_date: string;
  bbox: number[];
};

export async function previewProductPair(selection: PairSelection): Promise<ProductPairDownloadPreview> {
  return requestJson('/api/v1/downloads/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selection),
  });
}

export async function startProductDownload(
  selection: PairSelection & { role: 'pre_event' | 'post_event'; asset_key: 'Product' },
): Promise<ProductDownloadResponse> {
  return requestJson('/api/v1/downloads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(selection),
  });
}

export async function getProductDownload(downloadId: string): Promise<ProductDownloadResponse> {
  return requestJson(`/api/v1/downloads/${encodeURIComponent(downloadId)}`);
}

export type PreprocessingRequest = {
  pre_download_id: string;
  post_download_id: string;
  bbox: number[];
  target_crs: string;
  pixel_spacing_m: number;
  dem_name: 'Copernicus 30m Global DEM';
  allow_snap_auxiliary_downloads: boolean;
};

export async function startSarPreprocessing(request: PreprocessingRequest): Promise<SARPreprocessingResponse> {
  return requestJson('/api/v1/preprocessing', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
}

export async function getSarPreprocessing(jobId: string): Promise<SARPreprocessingResponse> {
  return requestJson(`/api/v1/preprocessing/${encodeURIComponent(jobId)}`);
}
