import { lazy, Suspense, useState } from 'react';

const ModelSettings = lazy(() => import('@/pages/user-setting/setting-model'));
const ProfileSettings = lazy(() => import('@/pages/user-setting/profile'));

export default function WorkspaceSettings({
  canAdminister,
  sharedWorkspace = false,
}: {
  canAdminister: boolean;
  sharedWorkspace?: boolean;
}) {
  const [tab, setTab] = useState<'profile' | 'models'>('profile');
  const showProfile = () => setTab('profile');
  const showModels = () => setTab('models');

  return (
    <section
      className="flex h-full min-h-0 flex-col gap-4 p-5"
      aria-label="설정"
    >
      <div>
        <h2 className="text-xl font-semibold">설정</h2>
        <p className="mt-1 text-sm text-text-secondary">
          {sharedWorkspace
            ? '모델과 API 연결은 공용 작업공간 전체에 적용됩니다.'
            : '기존 계정과 모델 설정을 그대로 사용합니다.'}
        </p>
      </div>
      {!sharedWorkspace && (
        <div className="flex gap-2" role="group" aria-label="설정 종류">
          <button
            type="button"
            onClick={showProfile}
            aria-pressed={tab === 'profile'}
            className="rounded-lg border border-border-button px-4 py-2 text-sm aria-pressed:bg-bg-card focus-visible:ring-2 focus-visible:ring-accent-primary"
          >
            내 계정
          </button>
          {canAdminister && (
            <button
              type="button"
              onClick={showModels}
              aria-pressed={tab === 'models'}
              className="rounded-lg border border-border-button px-4 py-2 text-sm aria-pressed:bg-bg-card focus-visible:ring-2 focus-visible:ring-accent-primary"
            >
              모델 · API 연결
            </button>
          )}
        </div>
      )}
      <div className="min-h-0 min-w-0 flex-1 overflow-auto">
        <Suspense
          fallback={
            <p role="status" className="p-4 text-text-secondary">
              설정을 불러오는 중입니다.
            </p>
          }
        >
          {(sharedWorkspace || tab === 'models') && canAdminister ? (
            <ModelSettings />
          ) : (
            <ProfileSettings />
          )}
        </Suspense>
      </div>
    </section>
  );
}
