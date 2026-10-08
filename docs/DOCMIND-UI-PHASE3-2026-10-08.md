# DocMind UI 3차 작업 기록 — 2026-10-08

기준 커밋: `7ec9dfc`. 검토 서버: <http://127.0.0.1:18081/docmind>.

사용자 요청에 따라 폴더 계층을 더 분명하게 표현하고 AI 질문을 문서 검색과 같은 입력 구조로 정리했다. 운영에는 배포하지 않는다.

## 적용 내용

- 폴더를 실제 중첩 목록으로 렌더링한다. 연결선, 열린/닫힌 폴더 아이콘, 상위 두 단계의 기본 펼침으로 위치를 구분한다. 긴 이름은 줄바꿈하고 깊은 계층의 들여쓰기는 제한한다.
- 필터는 일치한 폴더와 상위 경로를 함께 보여준다. 사용자가 직접 접은 상태는 필터 해제 후 복원한다. 선택된 폴더가 있는 경로는 기본적으로 펼친다.
- 실제 데이터의 하위 폴더 수와 문서 수만 표시한다. 상위 선택에 포함되는 하위 폴더 ID를 요청에 추가하지 않는다.
- AI 질문 시작 화면의 폭·제목·탭·입력창·범위 선택·질문 버튼을 문서 검색과 통일한다. 범위 필터는 제출 폼 밖에 둬 Enter로 질문이 전송되지 않게 한다.
- 답변은 말풍선 없이 제목·본문·출처 순서로 보여준다. 생성 중·완료·근거 부족·실패를 구분하고 기술적인 문맥 개수와 안내는 펼쳐 볼 수 있게 둔다.
- 긴 답변을 읽을 때 입력창과 검색 범위를 하단에 유지한다. 인용 열기·재선택·닫기의 가시 영역에 실제 입력창 높이를 반영한다.
- 범위 창은 입력창 위치와 남은 공간에 따라 위/아래로 열고, AI 화면의 상태 전환에도 위치를 다시 계산한다. 모바일은 화면 안쪽 여백을 둔 패널로 표시한다.
- HTML 표, 병합 셀, 기존 정화 처리와 근거 일치 조건, 원문 연결을 유지한다.

## 변경 파일

- `app/web/src/pages/docmind/index.tsx`, `index.less`: 공유 범위 선택, AI 시작 배치와 팝업 위치.
- `answer-panel.tsx`, `answer-panel.less`: 답변 가독성, 하단 입력창, 출처와 상태 표시, 인용 포커스 복원.
- `answer-evidence.tsx`: 근거 배치와 입력창에 가리지 않는 열기 처리.
- `scope-folder-picker.tsx`, `scope-folder-picker.less`: 실제 중첩 폴더 구조와 시각적 계층.
- `answer-panel.test.tsx`, `scope-folder-picker.test.tsx`, `split-search.test.tsx`: 인용 가림, 계층 선택, 입력/범위 분리 회귀.
- `docs/superpowers/plans/2026-10-08-docmind-ai-library.md`: 다음 자료관리 단계까지의 구현 순서.

## 검증

- 관련 Jest 16개 suite, 173개 테스트 통과. 인용 열기 가림 회귀는 실제 768/360px 좌표로 실패를 확인한 뒤 수정했다.
- 변경 TypeScript 파일 oxlint 및 UI diff 공백 검사 통과.
- 브라우저 응답은 동일한 고정 UI 검증 데이터를 사용한다. 실제 AI 생성을 호출한 검증이 아니다. 표 근거는 같은 실제 문서 청크로 비교한다.
- 폴더 선택, 중복 전송 차단, 범위 변경 시 무효화, 인용 연결, 원문 세션 및 HTML 처리 관련 기존 검사를 포함한다.
- 단위 테스트의 기존 PDF OCR 모달 i18next 초기화 경고는 남아 있다.

- Vite production build 성공. 기존 청크 크기/Tailwind 경고가 있다.
- 전체 TypeScript 진단은 기존 236개와 동일하며 새 진단은 0개다. 전체 타입 검사가 통과한 것은 아니다.
- 최종 360/768/1440px에서 페이지 가로 넘침과 콘솔/페이지 오류 0개. 360×640/768×640 범위 패널도 화면 내부에 표시된다.
- 긴 답변의 상단·중간·끝에서 하단 입력창이 보이며, 인용 열기와 닫기 후 근거 제목/원래 버튼이 입력창에 가리지 않는다.
- 범위 창을 열어 둔 채 AI 초기→생성 중, 완료→초기 상태로 바뀌어도 위치를 다시 계산한다. 모바일 패널은 16px 여백을 유지한다.
- 최종 브라우저 기록: `phase3-final-transitions.json`, `phase3-after-verification.json`. 수정 전 발견 기록은 `phase3-after-before-final-fixes-verification.json`으로 보존했다.
- 이 단계 진행 중 다른 작업의 로컬 LLM 관련 커밋 `70a7695`, `26a341e`가 추가되었다. 이번 UI 커밋에 포함시킨 변경이 아니다.

## 증거 보관

로컬 경로: `C:\Users\uplex\Documents\개발0921\docmind-ui-review`.

- 동일 내용·360/768/1440px 비교: `phase3-comparison.html`, `phase3-before-*.png`, `phase3-after-*.png`.
- 동일 UI fixture: `phase3-ai-fixture.json`. 실제 AI 생성이 아님을 화면에 표시했다.
- 테스트: `phase3-final-tests.log`, `phase3-typecheck.log`, `evidence-composer-red.log`, `evidence-composer-green.log`.

문서 내용·스크린샷·fixture는 Git에 넣지 않는다. 이 UI 변경은 백엔드·모델·프롬프트·API·인덱싱 파일을 포함하지 않는다. 진행 중 작업 폴더에 별도로 나타난 백엔드/설정/문서 변경은 보존하고 UI 커밋에서 제외한다.
