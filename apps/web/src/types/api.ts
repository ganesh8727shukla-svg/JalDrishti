export type HealthResponse = {
  status: string;
};

export type CaseStudyResponse = {
  schema_version: string;
  analysis_id: string;
  analysis_status: string;
  stage: string;
  event: {
    event_date: string;
    aoi_bbox_wgs84: [number, number, number, number];
  };
  source: {
    pre_scene_id: string;
    pre_acquisition_start: string;
    post_scene_id: string;
    post_acquisition_start: string;
    orbit_direction: string;
    relative_orbit: number;
    polarization: string[];
  };
  rasters: {
    pre_event: { quicklook_url: string; crs: string; width: number; height: number };
    post_event: { quicklook_url: string; crs: string; width: number; height: number };
    flood_mask: null;
    flood_probability: null;
    boundaries_geojson: null;
  };
  model: {
    status: string;
    version: null;
    inference_eligible: false;
    evaluation_status: string | null;
    candidate_checkpoint_sha256: string | null;
    deployment_approved: boolean;
  };
  results: { flooded_area_km2: null; crs: string; bbox_wgs84: [number, number, number, number] };
  infrastructure: null | {
    status: string;
    provider_snapshot_date: string;
    feature_counts_by_category: Record<string, number>;
    attribution: string;
    limitations: string[];
  };
  attribution: string[];
  warnings: string[];
};

export type ApiState = 'checking' | 'online' | 'offline';

export type ObservationMetadata = {
  scene_id: string;
  product_name: string | null;
  acquisition_start: string | null;
  acquisition_end: string | null;
  polarization: string[] | null;
  orbit_direction: string | null;
  relative_orbit: number | null;
  platform: string | null;
  asset_keys: string[];
};

export type ObservationPairCandidate = {
  rank: number;
  pre_scene_id: string;
  post_scene_id: string;
  elapsed_days: number | null;
  product_compatibility: 'match' | 'mismatch' | 'unknown';
  temporal_compatibility: 'match' | 'mismatch' | 'unknown';
  orbit_compatibility: 'match' | 'mismatch' | 'unknown';
  polarization_compatibility: 'match' | 'mismatch' | 'unknown';
  pre_aoi_coverage: 'full' | 'partial' | 'none' | 'unknown';
  post_aoi_coverage: 'full' | 'partial' | 'none' | 'unknown';
  common_aoi_coverage: 'full' | 'partial' | 'none' | 'unknown';
  verification_status: 'compatible' | 'questionable' | 'not_verified' | 'unsuitable';
  ranking_reasons: string[];
  warnings: string[];
};

export type ObservationPairSearchResponse = {
  status: 'completed' | 'no_data' | 'partial';
  pre_observation_count: number;
  post_observation_count: number;
  observations: ObservationMetadata[];
  pairs: ObservationPairCandidate[];
  warnings: string[];
};

export type ProductAssetPreview = {
  scene_id: string;
  role: 'pre_event' | 'post_event';
  product_name: string;
  asset_key: 'Product';
  acquisition_start: string | null;
  expected_size_bytes: number;
  provider_checksums: { algorithm: string; value: string }[];
};

export type ProductPairDownloadPreview = {
  pre_event: ProductAssetPreview;
  post_event: ProductAssetPreview;
  total_size_bytes: number;
  available_disk_bytes: number;
  warnings: string[];
};

export type ProductDownloadResponse = {
  download_id: string;
  scene_id: string;
  role: 'pre_event' | 'post_event';
  asset_key: 'Product';
  product_name: string | null;
  status: 'selected' | 'downloading' | 'downloaded' | 'integrity_validated' | 'geospatial_inspected' | 'failed' | 'validation_failed';
  progress_bytes: number;
  expected_size_bytes: number | null;
  file_size_bytes: number | null;
  error: { code: string; message: string } | null;
};

export type SARPreprocessingResponse = {
  job_id: string;
  status: 'selected' | 'running' | 'completed' | 'failed';
  stage: string;
  source_download_ids: string[];
  source_scene_ids: string[];
  outputs: Record<string, unknown> | null;
  model_input_status: string | null;
  warnings: string[];
  error: { code: string; message: string } | null;
};

export type FloodAnalysisResponse = {
  analysis_id: string;
  status: 'queued' | 'running' | 'blocked' | 'pending_independent_validation' | 'completed' | 'failed';
  stage: string;
  created_at: string;
  updated_at: string;
  preprocessing_job_id: string;
  event_id: string | null;
  event_date: string | null;
  model_version: string | null;
  model_checkpoint_sha256: string | null;
  source: Record<string, string | null>;
  results: {
    flooded_area_km2?: number | null;
    flooded_pixel_count?: number | null;
    valid_aoi_pixel_count?: number | null;
    crs?: string | null;
    bounds?: [number, number, number, number] | null;
    bounds_crs?: string | null;
    bbox_wgs84?: [number, number, number, number] | null;
  };
  rasters: {
    flood_mask?: null | {
      url: string;
      format: 'GeoTIFF';
      crs: string;
      bounds: [number, number, number, number];
      bounds_crs: string;
      bbox_wgs84: [number, number, number, number];
      nodata: number | null;
      sha256: string;
    };
    flood_probability?: null;
  };
  boundaries: null | { url: string; format: 'GeoJSON'; crs: 'EPSG:4326' };
  warnings: string[];
  error: null | { code: string; message: string };
};
