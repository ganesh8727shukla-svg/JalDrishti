import type { ApiState } from '../types/api';

type ApiStatusProps = {
  state: ApiState;
};

const labels: Record<ApiState, string> = {
  checking: 'Checking API',
  online: 'API connected',
  offline: 'API unavailable',
};

export default function ApiStatus({ state }: ApiStatusProps) {
  return (
    <span className={`api-status api-status--${state}`} role="status">
      <span className="api-status__dot" aria-hidden="true" />
      {labels[state]}
    </span>
  );
}
