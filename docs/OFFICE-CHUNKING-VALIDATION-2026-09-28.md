# Docling Office 청킹 연결 및 검증 — 2026-09-28

Asia/Seoul 기준. 기존 Word/Excel 청킹이 현재 클라우드 Docling 수집 분기에
연결되지 않았던 문제를 수정했다. DOC/DOCX/XLSX 코드는 `c895ded`, PPTX까지
연결한 코드는 `21f3494`다.

## 변경

- `ChunkService`의 parser-platform 분기에서 DOC/DOCX/XLSX/PPTX를 `OfficeChunker`로
  보낸다. PDF는 기존 Surya 청킹을 유지한다.
- DOC는 기존 Office sidecar에서 DOCX로 변환한 후 DOCX와 같은 Word 청킹을
  거친다. 기존 `_build_cks`/`_merge_cks`와 delimiter/soft token target을 재사용한다.
  이번 실제 표본의 설정은 `chunk_token_num=512`, `delimiter=\n`이다.
- 제목 경계가 바뀌면 병합을 끊고, 표·이미지는 별도 청크로 보존한다. 기존
  Word 정책과 같이 이미지 전후의 텍스트는 같은 제목 범위에서 병합할 수 있다.
  모든 청크가 최소 길이를 넘거나 최대 토큰 수 이하가 된다는 계약은 아니다.
- XLSX는 원본 바이트를 메모리에서 읽어 기존 행/셀 묶기와
  `whole_table_complete_rows_v1` 설정을 적용한다. Docling의 시트·셀 범위를
  사용하며 사용자 지정 시트 이름을 보존한다. 시트 또는 범위가 없으면 기존
  블록 청크를 유지한다. 표에 연결된 OCR 텍스트는 별도 청크에 한 번 보존한다.
- PPTX는 슬라이드별로 텍스트·목록·제목·캡션을 묶으며 다른 슬라이드와 합치지
  않는다. 표·이미지/OCR은 별도로 보존하고 모든 shape locator와 bbox를 유지한다.
- 병합 청크에 모든 기여 블록 ID, 원본 항목 ID, Office locator, provenance,
  OCR 연결과 토큰 수를 저장한다. 기존 단일 locator도 첫 기여 항목으로 유지한다.

## 코드·합성 검증

- 최종 집중 단위 테스트 11개 통과: DOC/DOCX/XLSX/PPTX 실제 수집 분기, Word
  이미지 전후 병합과 출처, Excel 셀 연결 및 표 OCR 중복 방지, 슬라이드 경계와
  그림/OCR 출처 보존 포함.
- 실행 중인 실제 Docling Office 서비스에 메모리 생성 DOCX/XLSX를 전달했다.
  DOCX는 기존 블록 청크 33개에서 4개로 줄었고 원본 항목 연결을 모두 보존했다.
  XLSX는 기존 큰 블록 4개에서 행 묶음 12개로 나뉘었으며 두 한글 시트 사이
  혼합이 없고, 헤더/데이터 100개 셀과 병합된 제목을 보존했다.
- 실제 Docling 서비스에 보낸 합성 PPTX는 3슬라이드, 텍스트 상자 36개,
  표 3개다. 기존 searchable 청크 39개가 6개로 병합됐고 슬라이드 혼합 0,
  텍스트 36개 및 기존 searchable shape locator 전체가 보존됐다. 합성 단색
  그림은 searchable 청크로 생성되지 않았으므로 실제 그림 OCR의 실증으로
  해석하지 않는다. 그림/OCR 병합 출처는 별도 단위 테스트로 확인했다.
- 제거 runtime 재유입 검사 통과. 새 파일 lint는 Windows bind mount를 실행
  가능 파일로 인식한 EXE002 외 오류가 없었으며 최종 E/F/I 검사도 통과했다.
  전체 테스트 통과라는 뜻은 아니다.

## 개발 서버 반영

기존 앱 이미지와 네 Compose 설정을 유지하고 변경 런타임 파일 4개를 read-only
mount했다. 앱만 재생성했으며 실행 파일 SHA-256이 checkout과 일치했다.
기존 동기화 작업 6개를 잠시 중지한 후 이전 활성 상태로 복원했다.
공용 세션과 검색 화면이 복구됐고, 승인된 TEST1 DOC를 명시적으로 재처리했다.
재처리 중 기존 활성 색인으로 브라우저 문서 검색 결과 5개/후보 8개를 확인했다.

## 실제 DOC 전후 비교

승인된 TEST1 문서 `7381ca897ce44a3a9f9185c22c260a81`만 재처리했다.
14:39:05에 생성된 ParserRun `fc79e2f80db2f17ba9391962cc654d07`이
14:45:29 KST에 활성화됐고, 14:45:53에 Windows 정리까지 COMPLETE가 됐다.
run 생성부터 최종 정리까지 약 6분 48초이며 전체 형식의 성능 목표가 아니다.
새 활성 청크 집합은 `5381543b6d0d6ddaa82f03ac6bbe1e3e`다.

| 지표 | 변경 전 | 변경 후 |
|---|---:|---:|
| 전체 청크 | 146 | 58 |
| 텍스트 청크 | 99 | 11 |
| 이미지/OCR 청크 | 41 | 41 |
| 표 청크 | 6 | 6 |
| 48토큰 미만 텍스트 | 98 | 1 |
| 16토큰 미만 텍스트 | 68 | 0 |
| 텍스트 길이 중앙값 | 9 | 99 |
| 토큰 수 메타데이터 누락 | 146 | 0 |
| provenance 누락 | 0 | 0 |

길이 비교는 전후 같은 multilingual-minilm/HuggingFaceTokenizer를 사용했다.
이는 Word 병합 정책의 토큰 계산 및 Document.token_num과 다른 지표다.
Document.token_num은 7,486→7,466이며 OCR 재실행이므로 본문 바이트 동일성을
주장하지 않는다. 146개 source item ID와 stable block ID 집합은 전후 동일했다.
이미지/표 개수와 모든 기여 블록 연결이 보존되고 텍스트 조각이 묶인 결과다.

작업 attempt 4/fence 7, job/Windows cleanup/Docker cleanup 모두 COMPLETE다.
ParserRun은 READY_WITH_WARNING이며 기존 경고
LEGACY_DOC_CONVERTED_TO_DOCX, DOCX_GEOMETRY_UNAVAILABLE, SURYA_MEDIA_EMPTY가
남아 있다. 이전 run은 RETAINED이며 전체 네 run의 raw artifact reference는 비었다.
비종료 작업·정리 미완료 작업은 0이고 Windows jobs와 앱 /tmp·임시 parser
경로, Surya /tmp의 잔여 파일은 0이었다. 승인 원본 DOC 네 개의 해시·크기·수정시각도 같았다.

최종 PPTX 반영 후 앱을 다시 생성하고 네 런타임 파일의 SHA-256 일치를 확인했다.
일시 중지했던 worker/watcher 4개는 Running, scoped 자정 작업 2개는 Ready로
복원했다. 브라우저를 새로고침한 뒤 `설치 매뉴얼`을 실제 제출한 결과는 다음과 같다.

| 범위 | 최종 결과 | 결합 후보 |
|---|---:|---:|
| 전체 | 5 | 40 |
| TEST1 폴더 및 하위 폴더 | 5 | 24 |
| 재처리한 DOC 1개 | 5 | 8 |

폴더/문서 범위에서 다른 부서 source 표시는 없었다. 이는 해당 질의의 응답·범위
검증이며 대표 한국어 검색 품질 평가나 모든 질의의 정확도 검증은 아니다.

## 지원 범위와 제한

현재 Docling Office 수집기의 지원 형식인 DOC/DOCX/XLSX/PPTX 네 형식 모두
구조별 청킹에 연결했다. 구형 XLS/PPT는 기존 parser-platform 자체의 미지원
형식이며 이번 변경으로 새 지원을 추가하지 않았다.

HWP/HWPX는 별도 RHWP sidecar에서 이미 HwpHybridChunker(Docling HybridChunker,
기본 최대 512, merge_peers=True)를 실행한다. adapter가 이미 병합된 청크와
단락·표 출처를 전달하므로 Office의 누락 분기와 다르다. 현재 서버는 관련
활성 환경 설정이 없고 기본값 false/false/off로 신규 HWP/HWPX 수집이 비활성이다.
이번에 HWP/HWPX를 활성화하거나 새 문서를 수집하지 않았다.

이번 실제 재색인은 DOC 한 개다. 나머지 기존 문서는 재처리해야 새 정책이
저장된 색인에 적용된다. DOCX/XLSX/PPTX는 실제 파서에 보낸 합성 문서 및
집중 회귀로 검증했으며 암호화된 실제 원본의 전체 수집 시험으로 확대하지 않는다.
