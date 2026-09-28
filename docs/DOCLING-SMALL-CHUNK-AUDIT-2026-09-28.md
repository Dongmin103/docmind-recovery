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

## 필요한 수정 범위

Office 검색 청크 생성 단계에서 인접 텍스트를 문맥과 길이에 따라 병합하는
처리가 필요하다. 원본 Docling 블록을 없애는 방식 대신 블록 ID·읽기 순서·
locator/좌표·제목/목록/표 경계·OCR attachment 출처를 유지해야 한다.
현재 단일 stable_block_id/source_item_id를 다중 기여 블록으로 보존하도록
계약도 함께 검토한다. PDF 병합 함수를 단순 호출하면 근거 정보가 손실될 수 있다.

구현 후 기존 승인 표본을 재처리하고 작은 청크 분포, 근거 연결, 전체/폴더/문서
검색 품질을 전후 비교해야 한다. 이번 조사는 해당 구현·재색인을 수행하지 않았다.
