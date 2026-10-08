# DocMind UI 1차 리디자인 검토 — 2026-10-08

## 범위와 기준

- 공통 화면 틀·공통 컨트롤 스타일·문서 검색 시작 화면만 구현했다. 사용자 후속 요청으로 설정 UI도 숨겼다.
- 기존 다크·민트 색상 토큰, React 18·TypeScript·Vite·Tailwind·LESS와 기존 Input을 재사용했다. 라이브러리를 추가하지 않았다.
- 최신 UI 기준 커밋: `280dac5f54a99f43805e13e9310fd677eb2cf1dc`. 작업 시작 시 이 worktree는 clean이었다.
- 작업 폴더: `C:/DocMindDev/docmind/.local/worktrees/local-llm-context`, 브랜치 `feat/local-llm-context`.
- 기본 checkout `C:/DocMindDev/docmind`의 미추적 사용자 문서 `docs/DEPT2-INGESTION-ERROR-LOG-2026-09-28.md`는 그대로 보존했다.
- 기본 로컬 서버 `127.0.0.1:18080`의 프런트는 최신 배포 UI보다 오래되었다. 최신 커밋의 기존 빌드와 수정 빌드를 별도 로컬 서버에서 같은 백엔드에 연결해 비교했다.

## 검토 주소와 산출물

- 수정본: http://127.0.0.1:18081/docmind
- 최신 기준본: http://127.0.0.1:18082/docmind
- 두 서버 모두 이 Windows 장비의 loopback에서만 실행되며, API는 기존 `127.0.0.1:18080`으로 전달한다.
- 스크린샷·로그·단일 HTML 비교 보고서: `C:/Users/uplex/Documents/개발0921/docmind-ui-review/`.
- 운영 컨테이너, Vercel 공개 사이트, 터널, DB·모델 설정은 변경하지 않았다. 승인 전 운영 반영은 하지 않는다.

## 확인한 기존 동작

| 화면 | 현재 동작과 보존 범위 |
|---|---|
| 문서 검색 | 전체/선택 폴더와 하위 폴더/선택 문서를 기존 payload로 검색한다. 빈 선택은 전송을 막는다. 명칭만 기존 검색 → 문서 검색으로 변경했다. |
| 검색 결과 | 기존 제목·메타정보·청크·원문 버튼·더 보기와 결과 무효화 로직을 유지했다. 본격적 결과 재배치는 하지 않았다. |
| AI 질문 | 질문·답변·입력 흐름, 고정 이웃 청크 범위, 인용→근거 연결, 원문 이동과 답변 상태를 유지했다. 본문/근거 스타일은 수정하지 않았다. |
| 원문 | 기존 TemporaryOriginalPreview와 문서 상세 연결을 유지했다. 원문 공급/복호화 코드는 변경하지 않았다. |
| 자료 관리 | 기존 라이브러리·처리 현황·선택 상태·All-in-One 안내를 유지했다. 공통 메뉴 외 재배치와 업로드/삭제 기능 추가는 하지 않았다. |
| 설정 | 메뉴와 DocMind 설정 렌더 경로 제거. `?view=settings`는 검색 화면을 표시한다. `/user-setting/*` 직접 주소는 `/docmind`로 리디렉션한다. 저장 설정과 백엔드 권한/API는 변경하지 않았다. |

## 공통 디자인 기준과 구현

- 배경 `--bg-base`, 작업 카드 `--bg-component`, 보조 표면 `--bg-card`, 구분선 `--border-button`을 재사용한다. dark 기준 배경 #161618, 카드 #202025.
- 본문 `--text-primary`, 보조정보 `--text-secondary`, 선택·포커스·주요 행동 `--accent-primary`를 사용한다. 전역 테마와 공통 UI 라이브러리는 수정하지 않았다.
- 메뉴 높이 52px, 사이드바 208px, 검색 컨트롤 최대 폭 768px. 4/8px 기반 간격, 작은 탭, 제목/설명/입력의 위계를 적용했다.
- 문서 검색 시작 상태에만 제목과 중앙 입력 카드를 적용한다. 검색 실행 후에는 기존 결과 영역 폭과 배치를 유지한다.
- 검색 방식·범위·입력을 가까이 묶었다. 근거 없는 예시 질문·문서 수·최근 검색을 추가하지 않았다.
- 768px 미만에서는 메뉴를 접고 펼친다. 모바일 토글과 메뉴 항목 최소 높이 44px, aria-expanded/controls, Escape 닫기 및 포커스 복귀를 지원한다.
- 범위 팝업의 기존 선택 동작을 유지하고 닫기 버튼·Escape·포커스 복귀를 추가했다. 팝업은 불투명 작업 표면 토큰을 쓴다.

## 변경 파일

| 파일 (app/web/src 기준) | 변경 |
|---|---|
| pages/docmind/workspace-shell.tsx | 간결한 헤더/메뉴, 설정 제거, 모바일 접기 |
| pages/docmind/workspace-shell.less | 공통 화면 틀 토큰·간격·포커스·반응형 |
| pages/docmind/workspace-shell.test.tsx | 모바일 접근성·권한별 메뉴 회귀 |
| pages/docmind/index.tsx | 시작 제목·입력 배치·범위 닫기·설정 렌더 제거 |
| pages/docmind/index.less | 검색 시작·탭·컨트롤 스타일 |
| pages/docmind/index.test.tsx | 제거한 설정 mock 정리 |
| pages/docmind/split-search.test.tsx | 탭 명칭 갱신·Escape 후 선택 범위 보존 회귀 |
| pages/docmind/workspace-views.tsx | 설정 lazy import 제거 |
| pages/docmind/workspace-settings.tsx | DocMind 전용 설정 UI 삭제 |
| utils/docmind-workspace.ts / .test.ts | 허용 섹션에서 설정 제거·직접 진입 회귀 |
| routes.tsx / routes.test.tsx | 이전 사용자 설정 주소 리디렉션·회귀 |

## 검증 결과

- 최신 소스의 변경 전 Jest: 5 suites / 91 tests 통과.
- 최종 Jest: 8 suites / 111 tests 통과. 검색 범위·AI 요청·인용·근거·메뉴·직접 설정 URL 회귀 포함.
- 변경된 TS/TSX 파일 oxlint: exit 0. `git diff --check`: 통과.
- 최종 production build: 성공. 기존 Tailwind plugin/대형 chunk 경고는 남아 있다.
- 전체 TypeScript 검사: 변경 전 236건, 변경 후 236건으로 진단 내용이 정확히 동일하다. 새 진단은 0건이며, 전체 type-check가 통과한 것은 아니다.
- 프로젝트 필수 retired-runtime 검사: 통과. 관련 독립 스크립트 테스트 7개 통과. 로컬 기본 Python에는 pytest가 없고 backend 전역 fixture에는 nltk/async plugin이 없어, 기존 pytest 환경에서 해당 동기식 테스트만 최소 설정으로 분리해 실행했다.
- 실제 브라우저: 360×1000 / 768×1000 / 1440×1000에서 가로 넘침 0. 범위 팝업이 화면 안에 들어오며, 키보드 탭 전환·Escape 복귀·모바일 메뉴 동작을 확인했다.
- 실제 API 검색: '스마트공장', 전체 범위, HTTP 200/code 0, 결과 5개 표시.
- 빈 결과/오류 화면은 브라우저 테스트 내 통제 응답으로 확인했다. 실제 제품 데이터나 API 계약에 fixture를 넣지 않았다.
- 정상 검증 실행의 콘솔 오류·page error·실패 요청은 모두 0. 별도 재캡처 한 차례에서 공유 세션 준비 중 401이 관찰되어, 준비 완료를 기다린 후 정상 화면을 재확인했다. 인증 코드는 변경하지 않았다.
- 실제 유료 AI 생성/원본 복호화를 추가 실행하지 않았다. 해당 동작의 보존은 기존 코드 diff와 회귀 테스트 기준이며, 원문 렌더링 전체를 새로 실서버 검증했다고 주장하지 않는다.
- 같은 최신 기준 소스·빈 검색어·공유 작업공간·동일 화면 크기로 before-current/after 스크린샷을 확보했다. AI 질문·자료관리 초기 화면도 기록했다.
- 독립 코드 리뷰의 설정 직접 URL 노출 지적을 수정했고 재검토에서 추가 지적은 없었다.

## 구현 순서와 다음 검토

코드/배포 기준 확인 → 기준 캡처 및 테스트 → 공통 화면 틀/검색 시작 구현 → 설정 숨김 요청 반영 → 독립 리뷰 → 빌드/브라우저 검증 → 단계 커밋.

이번 단계는 디자인 검토용이다. 사용자가 1차 방향을 승인한 뒤 검색 결과·원문, AI 답변·근거, 자료관리 세부 화면으로 순차 확장한다.

## 참고

- Linear: https://linear.app/now/behind-the-latest-design-refresh
- Glean: https://www.glean.com/enterprise-search
- Dropbox Dash: https://dash.dropbox.com/features/universal-search
- NotebookLM: https://blog.google/innovation-and-ai/models-and-research/google-labs/notebooklm-discover-sources/

공식 소개 자료의 시각적 원칙만 참고했으며 해당 제품의 기능을 새로 추가하지 않았다.
