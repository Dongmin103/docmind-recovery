# DocMind UI 2차 작업 기록 — 2026-10-08

기준 커밋: `210b5b72804ea4ca995409fc9447474a6d8f1cf9` (1차 승인본).
검토 서버: <http://127.0.0.1:18081/docmind>. 운영 배포는 하지 않았다.

## 적용 범위

사용자가 1차를 승인하고 2차 검색결과·원문 화면 진행을 요청했다. 작업 중 색상 변경을 허용하고, 템플릿처럼 보이는 장식을 줄이는 방향과 폴더 선택의 시각적 구조화를 추가 요청했다.

- 공통 색상은 중립적인 그래파이트 배경, 밝은 본문, 차분한 블루 강조로 조정했다. 사이드바 선택은 회색 면으로 표시하고 색상은 주요 행동·포커스·선택 표시에 제한했다.
- 검색결과는 제목을 한 번만 보여주고 경로·위치·관련 내용 순서로 배치했다. 큰 순위와 반복 제목, 박스형 장식을 줄였다. 없는 위치와 후보 수는 만들어 표시하지 않는다.
- 표는 문장처럼 평탄화하지 않는다. 기존 ChunkContent로 행·열·병합 셀을 보존하고, 좁은 화면에서는 표 내부만 가로 스크롤한다. 일반 텍스트만 발췌 표시하며, AI 답변·인용 근거의 기존 HTML 렌더링은 유지한다.
- 결과 화면의 검색창·범위 선택을 상단에 유지한다. 검색 중·빈 결과·실패·범위 변경 상태를 구분한다.
- 1200px 이상에서는 선택한 결과 옆에 원문 패널을 열고, 좁은 화면에서는 같은 DOM을 전체 화면 모달로 승격한다. `원문 보기`를 눌러야 기존 미리보기 요청을 시작한다.
- 선택한 문서·버전이 같으면 원문 세션을 재사용한다. 새 검색·질문 수정·범위 변경·화면 이탈 시 이전 선택을 해제한다. Escape와 닫기는 결과 버튼으로 포커스를 돌린다.
- 원문 처리에 따른 자동 근거 갱신은 원문 세션을 유지한다. 갱신 중·실패에는 이전 근거와 하이라이트를 숨기고 상태를 표시하며, 성공하면 새 근거로 교체한다.
- 폴더 선택은 계층선, 펼침·접기, 이름·경로 필터, 선택 수, 상위 폴더에 포함된 상태를 표시한다. 체크한 ID만 기존 요청에 전달하며 하위 ID를 임의로 추가하지 않는다. 서버가 제공한 문서 수만 표시한다.

AI 답변 본문과 자료관리의 본격적인 배치는 이번 단계에서 변경하지 않았다. 공통 테마는 동일하게 적용되며 설정 숨김은 유지한다. 검색·RAG·모델·API·데이터 계약과 인덱싱 코드는 변경하지 않았다.

## 변경 파일

- `app/web/src/pages/docmind/index.tsx`: 결과 정보 위계, 범위 선택기 통합, 선택·원문 상태 연결.
- `app/web/src/pages/docmind/index.less`: 결과 목록, 검색 상태, 상단 제어, 원문 패널, 좁은 화면 범위 창.
- `app/web/src/pages/docmind/workspace-theme.less`, `workspace-shell.less`: DocMind 내부 색상 토큰과 공통 메뉴.
- `app/web/src/pages/docmind/search-result-content.tsx`, `.test.tsx`: 표 구조 보존과 일반 텍스트 발췌 표시.
- `app/web/src/pages/docmind/search-preview.tsx`: 기존 TemporaryOriginalPreview를 감싸는 반응형 native dialog.
- `app/web/src/pages/docmind/scope-folder-picker.tsx`, `.less`: 실제 폴더 데이터로 구성하는 선택 목록.
- `app/web/src/pages/docmind/split-search.test.tsx`, `index.test.tsx`, `scope-folder-picker.test.tsx`: 선택·세션·범위 회귀와 계층 선택 검증.
- `docs/superpowers/plans/2026-10-08-docmind-results.md`: 범위와 검증 계획.

## 검증 기록

- 관련 Jest 11개 suite, 137개 테스트 통과. 새 선택·Escape·범위 해제·메타데이터 누락 및 자동 갱신 성공/실패 회귀는 실패를 확인한 뒤 구현했다. 기존 원문 세션 생성·취소·정리 테스트도 포함한다.
- 변경 TypeScript 파일 oxlint 및 `git diff --check` 통과.
- Vite production build 성공. 기존 청크 크기 및 Tailwind 플러그인 경고는 남아 있다.
- 전체 TypeScript 검사에는 기존 236개 진단이 있으며 1차 기준과 동일하다. 추가 진단은 0개다. 전체 검사가 통과했다고 보지는 않는다.
- 실제 `스마트공장` 검색: HTTP 200/code 0, 결과 5개. 원문 선택만으로 생성 요청이 나가지 않는 것을 확인했다.
- 실제 PDF 원문: POST 202 → 대기/복호화 → READY → content 200, canvas 렌더링. 패널 닫기에서 DELETE 200을 확인했다.
- 실제 원문을 연 상태의 데스크톱↔모바일 전환에서 동일 DOM과 세션을 유지하고 생성 요청은 1회였다. 좁은 화면에서 배경 포커스가 차단되며, 브라우저 크롬을 거친 Tab은 모달로 돌아온다.
- 독립 코드 검토에서 발견한 자동 근거 갱신 시 원문이 닫히는 회귀를 수정하고 성공·실패 테스트를 추가했다. 최종 재검토에서 중요한 미해결 회귀는 없었다.

## 화면 증거

증거 경로: `C:\Users\uplex\Documents\개발0921\docmind-ui-review`.

- 변경 전 결과/패널: `phase1-supplement-before-phase2-results-{360,768,1440}.png`, `phase1-supplement-before-phase2-preview-{360,1440}.png`.
- 실제 원문 및 세션 검증: `phase2-browser-verification.json`, `phase2-focus-detail.json`.
- 최종 테스트/빌드: `phase2-final-tests.log`, `phase2-final-typecheck.log`, `phase2-build.log`.

스크린샷·원문·실제 문서 내용과 브라우저 상태는 Git에 넣지 않는다. 최종 화면 비교 및 범위 선택 검증 기록은 같은 로컬 증거 폴더에 저장한다.

## 최종 화면 확인

- 360/768/1440px에서 페이지 가로 넘침 없음. 폴더 창은 360×640, 768×640을 포함해 화면 내부에 표시된다.
- 실제 54개 폴더 목록에서 상위 선택·하위 포함 표시·접기·필터와 선택 유지, 서버로 전달되는 정확한 folder_ids를 검증했다.
- 실제 HWP 표의 14개 행과 rowspan 2/4/2/3을 그대로 보존한다. 모바일에서는 표 컨테이너 안에서만 스크롤한다.
- AI 인용은 같은 실제 소스 청크를 사용하는 브라우저 전용 응답으로 검증했다. 실제 AI 생성은 실행하지 않았으며 증거 화면에 검증용이라고 표시했다.
- 최종 화면의 로딩·빈 결과·오류, 범위 선택 및 HTML 표 점검에서 콘솔/페이지/네트워크 실패는 0개였다.
- 원문 패널의 표 외 HTML·Markdown 강조도 별도 브라우저 응답으로 검증했다. 리디자인 후 일반 태그를 문자 그대로 노출하지 않는다.
- 변경 전후 비교: 로컬 `phase2-comparison.html`. 결과·시작·원문 화면은 같은 크기로 비교하고, 폴더 선택은 최종 화면을 제공한다.
- 최종 추가 증거: `phase2-final-verification.json`, `phase2-final-table-verification.json`, `phase2-excerpt-verification.json`. 첫 파일에 기록된 시작 팝업 높이 문제는 두 번째 파일의 재검증에서 해결되었다.
