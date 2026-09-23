import FallbackComponent from '@/components/fallback-component';
import { DocMindSharedWorkspaceError } from '@/utils/docmind-shared-workspace';
import { useEffect, useState } from 'react';
import { useRevalidator, useRouteError } from 'react-router';

export default function DocMindWorkspaceError() {
  const error = useRouteError();
  const revalidator = useRevalidator();
  const [attempts, setAttempts] = useState(0);
  const recoverable =
    error instanceof DocMindSharedWorkspaceError && error.status >= 500;

  useEffect(() => {
    if (!recoverable || revalidator.state !== 'idle' || attempts >= 12) return;
    const timer = window.setTimeout(() => {
      setAttempts((count) => count + 1);
      void revalidator.revalidate();
    }, 5000);
    return () => window.clearTimeout(timer);
  }, [recoverable, revalidator.state, revalidator.revalidate, attempts]);

  const retry = () => {
    setAttempts(0);
    void revalidator.revalidate();
  };

  if (!recoverable) return <FallbackComponent />;
  return (
    <main className="mx-auto max-w-xl p-8 text-text-primary">
      <h1 className="text-xl font-semibold">
        작업공간 연결을 기다리고 있습니다
      </h1>
      <p role="alert" className="mt-3 text-text-secondary">
        {attempts < 12
          ? '서버가 준비되면 자동으로 다시 연결합니다.'
          : '서버 연결이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.'}
      </p>
      <button
        type="button"
        onClick={retry}
        disabled={revalidator.state !== 'idle'}
        className="mt-5 rounded border border-border-button px-4 py-2 disabled:opacity-50"
      >
        다시 연결
      </button>
    </main>
  );
}
