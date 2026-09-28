# Docling Office의 작은 검색 청크 조사

2026-09-28, Asia/Seoul. 사용자 요청에 따라 GPT-6 Sol high 서브에이전트가
코드·Git 이력·실행 컨테이너·Elasticsearch를 읽기 전용으로 조사했다.
청킹 정책 수정, 원본 변경, 재처리, 컨테이너 재시작은 하지 않았다.

## 결론과 기존 코드

현재 DOC/DOCX 수집 경로는 작은 Docling 텍스트 블록을 합치지 않는다.
작은 청크를 그대로 두자는 사용자 합의를 확인한 것은 아니다.

- `app/rag/svr/task_executor_refactor/chunk_service.py:232`의 parse_run_id
  경로는 PDF만 SuryaHybridChunker로 전달한다. 나머지는
  CommonToStandardChunkAdapter를 호출한다.
- `app/rag/parser_platform/standard_bridge.py:299`의 adapter는 검색 가능한
  ParsedBlock 하나당 검색 청크 하나를 만든다. Office OCR attachment는 부모
  블록에 붙지만 인접 텍스트 블록 병합은 없다.
- `app/rag/parser_platform/surya_hybrid_chunker.py:602`에는 짧은 청크 병합이
  있다. 기본 최소 48/최대 512토큰이며 호환되는 인접 청크에 적용하고 표 등은
  별도 처리한다. 모든 청크의 최소 길이를 무조건 보장하는 규칙은 아니다.
- `app/rag/flow/compiler/compiler.py:238`에도 작은 텍스트 병합 코드가 있으나
  별도의 flow Parser/명시적 rechunk 경로다. 현재 Office 수집 경로는 호출하지 않는다.

위 PDF/Office 분기와 병합 코드는 최초 소스 인수 커밋 `7126a79`부터 존재한다.
현재 Git 이력에서 이후 Office 경로에 병합을 연결한 변경은 확인되지 않았다.
인수 이전 저장소의 수정 시도나 대화까지 없었다고 단정하지 않는다.

### 사용자 확인 요청에 따른 기존 DOCX/XLSX 경로 추가 조사

사용자는 XLSX와 DOCX 병합을 이미 처리했던 것으로 기억한다고 설명했다.
추가 조사에서 그 기억과 양립하는 기존 구현을 확인했다. PDF 병합만 설명하고
Office 병합 구현 자체가 없었던 것처럼 해석하게 한 앞선 설명은 불완전했다.

- 일반 DOCX 경로 `app/rag/app/naive.py:1091`은 Docx()가 만든 sections를
  `naive_merge_docx`로 합친다. 연속 텍스트를 설정 토큰 수까지 묶으며 표·이미지는
  구분한다. 호출 기본값은 128이고 사용자 delimiter 등에 따라 조건이 달라진다.
- 일반 XLSX 경로 `app/rag/app/naive.py:1180` 및 `:1387`에는
  excel_field_chunking 설정에 따른 전용 청킹이 있다.
  `app/rag/app/excel_chunker.py:517`의 chunk_excel_record_groups는 행 fragment를
  토큰 예산에 맞춰 묶으며 원본 source_cells를 보존한다. 완전한 행 정책을
  선택하면 제목/헤더 문맥과 명시한 embedding tokenizer/budget도 적용한다.
- 두 구현 모두 `7126a79` 인수 스냅샷에 이미 포함돼 있다. 당시 사용자가 시험한
  설정과 실행 결과까지 이 Git 이력만으로 확인할 수는 없다.

현재 클라우드의 parse_run_id 분기는 위 일반 청커를 호출하는 else 분기로
들어가지 않는다(`chunk_service.py:269`). 따라서 정확한 문제는 **기존 DOCX/XLSX
병합 구현의 부재가 아니라, 현재 Docling Office 수집 경로에서 그 처리를 거치지
않는다는 것**이다. 현재 DOC 실측을 과거 XLSX/DOCX 작업 전체의 실패로 확대하지 않는다.

실행 앱 이미지 `sha256:47f9f8d9843102cc9a853bde0dd339326cf271c201d634262957cb4a782d5660`
내 chunk_service.py, standard_bridge.py, surya_hybrid_chunker.py는 현재 checkout과
SHA-256이 일치한다. Docling adapter는 현재 소스의 read-only mount다.
따라서 이번 차이는 이 세 파일의 배포 누락으로 설명되지 않는다.

## 활성 색인 집계

대상 문서 `7381ca897ce44a3a9f9185c22c260a81`, 활성 청크 집합
`5f95211bf7d5a10afa55615a41f80a2d`를 읽었다. 원문/청크 본문은 출력·저장하지 않고
메모리에서 PDF 청커와 같은 multilingual-minilm/HuggingFaceTokenizer.count_tokens로
길이만 집계했다. 이는 Document.token_num 및 BGE 토큰 수와 다른 지표다.

| 종류 | 전체 청크 | 48토큰 미만 |
|---|---:|---:|
| 텍스트 | 99 | 98 |
| 이미지/OCR | 41 | 6 |
| 표 | 6 | 0 |
| 합계 | 146 | 104 |

텍스트 청크 68개는 16토큰 미만이며 텍스트 중앙값은 9토큰이다.
저장된 parser_platform.chunk_token_count는 146개 모두 null이었다.
표본 하나의 실측이며 전체 문서군의 품질 판정으로 확대하지 않는다.

## 필요한 수정 범위 — 이후 구현 완료

아래는 조사 당시 제안이다. 이후 DOC/DOCX/XLSX/PPTX의 형식별 청킹을 연결했고,
실제 DOC의 텍스트 청크가 99개에서 11개로 병합되는 것을 확인했다.
최종 코드·합성 시험·실제 재색인·정리 결과는
[Office 청킹 검증](OFFICE-CHUNKING-VALIDATION-2026-09-28.md)을 따른다.

우선 기존 DOCX/XLSX의 청킹 정책과 적용 설정을 기준으로 현재 Office 경로에
연결할 수 있는 부분을 확인한다. PDF의 48~512 정책을 모든 Office 형식에 새로
일괄 적용하는 것으로 결정한 것이 아니다. 원본 Docling 블록을 없애는 방식 대신 블록 ID·읽기 순서·
locator/좌표·제목/목록/표 경계·OCR attachment 출처를 유지해야 한다.
현재 단일 stable_block_id/source_item_id를 다중 기여 블록으로 보존하도록
계약도 함께 검토한다. PDF 병합 함수를 단순 호출하면 근거 정보가 손실될 수 있다.

구현 후 기존 승인 표본을 재처리하고 작은 청크 분포, 근거 연결, 전체/폴더/문서
검색 품질을 전후 비교해야 한다. 이 최초 조사 자체는 해당 구현·재색인을 수행하지
않았으며, 이후 진행한 구현과 검증은 위 후속 보고서에 기록했다.
