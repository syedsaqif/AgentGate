import type { ApiError } from "../api/errors";

export function ErrorNotice({ error }: { error: ApiError }) {
  return (
    <div className="notice notice-error" role="alert">
      <p>{error.message}</p>
      <dl>
        <div><dt>Code</dt><dd>{error.code}</dd></div>
        {error.requestId && <div><dt>Request ID</dt><dd>{error.requestId}</dd></div>}
      </dl>
    </div>
  );
}
