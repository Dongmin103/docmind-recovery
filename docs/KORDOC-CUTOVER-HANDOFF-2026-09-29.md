# Kordoc 파싱 엔진 전환 인수인계 (2026-09-29)

> 이 문서는 구현 전 조사 시점의 기록이다. 이후 코드 전환 상태는 [Kordoc 검색 파서 전환 구현 상태](KORDOC-CUTOVER-IMPLEMENTATION-2026-09-29.md)를 참조한다.

## 현재 상태

사용자 요청에 따라 여기서 구현을 멈췄다. **운영 전환 완료 상태는 아니다.** 이 작업의 수정 사항은 현재 작업 트리에 있고 새 커밋이나 운영 배포는 하지 않았다. 같은 작업 트리에 수집 복구, 화면, Windows 도구 등 다른 작업의 수정 사항도 섞여 있으므로 전체 파일을 일괄 스테이징하거나 되돌리지 말 것.

Kordoc 4.15.7을 파싱 엔진으로 선택했다. DOCX 삽입 이미지와 PDF OCR은 Kordoc의 `parseImage`/`parse`를 사용한다. DOC는 LibreOffice로 DOCX 변환 후 Kordoc, PPTX는 LibreOffice로 PDF 변환 후 Kordoc에 넣는 코드를 추가했다. XLS/XLSX는 Kordoc의 시트명·압축된 표·병합 구조를 그대로 사용한다. 원본 `C3` 같은 셀 주소 표시는 이번 전환에서 제공하지 않기로 했다. DOC 변환에서 표가 문단으로 풀리는 사례는 사용자가 이번에는 넘어가기로 했다.

## 구현된 연결

| 형식 | 현재 작업 트리의 Kordoc 경로 | 확인 상태 |
| --- | --- | --- |
| PDF | Kordoc `parse(..., {ocr:true})` → 페이지 좌표 블록 → Python 청킹 | 합성 텍스트·스캔 PDF 검사 통과 |
| DOCX | Kordoc `parse` → 삽입 이미지는 같은 패키지의 `parseImage` → Word 청킹 | 합성 문서·이미지 OCR 검사 통과 |
| XLS/XLSX | Kordoc 표 IR → 행 단위 청킹. 원본 셀 주소 재읽기 없음 | 합성 XLS/XLSX 검사 통과. 큰 병합 표 품질 미검증 |
| HWP/HWPX | Kordoc IR → 공통 블록 → 문서 청킹 | HWPX 합성 문서 검사 통과. 바이너리 HWP 미검증 |
| DOC | LibreOffice DOCX 변환 → Kordoc → Word 청킹 | 합성 DOC 검사 통과. 표 구조 평탄화는 허용된 제한 |
| PPTX | LibreOffice PDF 변환 → Kordoc OCR → 슬라이드 번호 기반 청킹 | 한 장짜리 합성 PPTX 검사 통과. 다중 슬라이드·도형·노트 미검증 |

엔진 선택은 `app/rag/parser_platform/dispatch.py`, 공통 결과 변환은 `app/rag/parser_platform/kordoc_office_pilot.py`, 형식별 청킹은 `app/rag/parser_platform/office_chunker.py`, Node 파싱·변환은 `app/parser_services/kordoc/src/parse.mjs`와 `convert.mjs`에 있다. `app/docker/docker-compose-parser-platform.yml`은 형식별 Kordoc 스위치를 기본 `1`로 설정하고 `kordoc-parser` 서비스를 추가한다. 단, 상위 `PARSER_PLATFORM_ENABLED`와 `PARSER_PLATFORM_INTEGRATION_READY`는 기본 `0`이므로 Compose 파일을 빌드했다고 곧바로 수집이 켜지지는 않는다. Python 설정만 직접 만들면 Kordoc 스위치의 기본값은 아직 `False`다.

## 기존 엔진 의존성 확인

- `rhwp-python`은 **Kordoc 경로에 필요 없다.** 현재 `rhwp-parser` 서비스와 예전 HWP 경로가 저장소에 남아 있어 검색 결과에 나타난다. HWP/HWPX Kordoc 전환의 실제 운영 검증 후 해당 컨테이너와 승격 설정을 제거해야 한다.
- Surya OCR 컨테이너는 Kordoc으로 선택된 PDF/DOCX/DOC/XLS/XLSX/PPTX 경로에서 호출하지 않는다. 수집 작업의 사전 Surya 준비 검사도 이 형식들의 Kordoc 스위치가 켜지면 건너뛰도록 바꿨다. 예전 PDF/Office 경로와 Compose 선택 프로필은 아직 남아 있다.
- 예전 PDF `SuryaHybridChunker`는 이름과 달리 Python 청킹 코드이며 `docling_core`에 의존한다. Kordoc PDF는 이 청커 대신 `OfficeChunker`의 페이지 경계 기반 청킹으로 연결했다. **기존 PDF 청킹과 결과가 완전히 같다는 검증은 아직 없다.**
- Docling 파싱 서비스는 Kordoc 선택 경로에서 호출하지 않는다. DOC/PPTX 변환은 Kordoc 컨테이너 안의 LibreOffice가 수행한다. 예전 Docling 서비스와 `ParserPlatformStandardBridge.from_config()`의 클라이언트 생성 코드는 아직 남아 있다.
- 기존 Excel 청커의 `RAGFlowExcelParser` 재읽기 코드는 예전 Docling 분기에만 남는다. Kordoc XLS/XLSX 분기는 원본 워크북을 다시 열지 않는다. 이 결정에 따라 Kordoc 청크에는 `source_cells`와 정확한 원본 셀 주소가 없다. [Kordoc의 시트 블록 구현](https://github.com/chrisryugj/kordoc/blob/main/src/xlsx/sheet-blocks.ts)을 근거로 했다.

## 검증 증거

- 로컬 신규 이미지 `docmind-kordoc-parser:cutover-local` 빌드 성공. 크기 571,038,581바이트. 기존 시험 이미지 408,864,163바이트. LibreOffice Writer/Impress를 함께 넣으면서 약 162MB 증가했다. 이 이미지는 로컬 Docker에만 있으며 배포 태그·레지스트리에는 없다.
- 신규 이미지, 네트워크 차단, 읽기 전용 OCR 모델 마운트에서 Node 파서 시험 **10/10 통과**. DOCX 이미지 OCR과 스캔 PDF OCR 포함.
- Python `test_kordoc_office_pilot.py` **10/10 통과**. XLSX는 원본 셀 주소 없이 표로 청킹함을 확인했다.
- 실제 Kordoc 4.15.7 결과를 Python 정규화·청킹에 연결한 합성 DOC/PPTX/XLS/HWPX 각각 1건은 모두 완료됐다: DOC 3블록→1청크, PPTX 1→1, XLS 1→1, HWPX 2→1.
- 관련 Python 시험 묶음은 **30개 통과, 3개 실패**였다. 세 실패는 기존 시험이 참조하던 `structured.docx` 합성 fixture가 없어서 발생했다. 참조를 존재하는 `office-sample.docx`로 수정했지만, 수정 뒤 해당 묶음을 다시 실행하지 않았다. `test_office_chunker.py`를 포함한 묶음은 기존 `deepdoc.parser.pdf_parser` 시험 stub의 `PlainParser` 누락으로 수집 중단됐다.
- `py_compile`(변경한 주요 Python 파일)과 `git diff --check`는 통과했다. Compose 전체 구성 검사, 앱 통합 수집, 검색 활성화, 운영 문서, 처리 시간·최고 RAM 측정은 하지 않았다. 앞선 대화에서 성능 시험은 1회만 하기로 했으며 이번 단계에서 추가 성능 수치는 만들지 않았다.

## 다음 작업

1. 위 실패한 시험 묶음을 다시 실행하고, `test_office_chunker.py` 시험 stub 문제를 해결한다. 엔진 선택 테스트에 DOC/PPTX/XLS/HWP도 포함한다.
2. Kordoc HTTP 서비스부터 API 수집·청크 활성화까지 격리된 통합 시험을 진행한다. 특히 다중 슬라이드 PPTX, 큰/병합 XLS(X), 실제와 같은 HWP 바이너리와 PDF 표를 합성 입력으로 확인한다.
3. Kordoc PDF 청킹이 기존 C128·페이지 출처·표 검색 계약을 충족하는지 비교한다. 현재 전환은 HybridChunker를 제거했으므로 이 검증이 필수다.
4. 폐쇄망 배포 경로에 `ppocr/{det.onnx,rec_korean.onnx,rec_korean.yml}` 모델을 준비하고 `KORDOC_OCR_MODEL_DIR`을 지정한다. 모델은 현재 사용자 임시 디렉터리에만 있고 Git/이미지에는 없다. 모델이 없으면 Kordoc `/health`가 503을 반환한다.
5. Compose 전체 구성 및 신규 이미지의 실 배포 빌드를 확인한다. 현재 Kordoc 서비스 설정은 2 vCPU/3 GiB와 `/tmp` 1 GiB이며 최고 사용량은 미측정이다. 운영 설정과 활성화는 별도로 결정한다.
6. 통합 검증 후 남은 Surya/Docling/rhwp 선택 프로필과 오래된 파서 분기, `pilot` 명칭, 과거 문서를 정리한다. 기존 `docs/KORDOC-CUTOVER-READINESS-2026-09-29.md`는 이번 직접 XLS 처리·PPTX 변환 결정보다 오래된 설명을 포함한다.
7. 사용자가 요청한 처리 흐름 시각화는 변경·검증을 마친 시점에 서브에이전트에게 맡긴다. 이번에는 사용자가 구현을 멈추고 인수인계 문서를 작성하라고 했으므로 시각화를 요청하지 않았다.

## 작업 트리 주의

커밋하지 않은 Kordoc 관련 파일과 무관한 다른 작업의 수정이 같은 작업 트리에 공존한다. 필요한 경우 Kordoc 관련 파일의 실제 diff만 선별해 리뷰·스테이징할 것. 시험 중 생성한 DOC/PPTX/XLS/HWPX와 결과 JSON은 사용자 임시 디렉터리 `docmind-kordoc-cutover`에 있으며 저장소에 넣지 않았다.
