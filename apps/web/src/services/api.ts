import type { HealthResponse } from '../types/api';

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
