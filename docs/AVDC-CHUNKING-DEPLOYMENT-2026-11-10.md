# AVDC 청킹 2차 후보 운영 적용 — 2026-11-10

사용자 승인으로 검증한 PDF 청킹 2차 후보를 AVDC 운영 API에 적용했다. Kordoc 코드·버전·분류·읽기 순서와 OpenVINO·검색 설정은 유지했다. Windows PC의 GPU 운영 컨테이너는 변경하지 않았다.

## 적용 내용

- 짧은 제목을 다음 본문과 묶고, 표 경계 때문에 제목만 분리되는 1차 후보의 회귀를 수정했다. 표는 독립 청크로 보존한다.
- 가까운 두 줄 인용 제목을 연결하고, 짧은 낱글자 블록 사이에 불필요한 줄바꿈을 추가하지 않는다. 반복 문구를 삭제하거나 파서의 다단 분류를 다시 만들지 않는다.
- DocMind 청킹 식별자를 `docmind-kordoc-chunker-v3`으로 올렸다. 기존 청킹 설정의 fingerprint와 구분하며, Kordoc 파서 버전 변경은 아니다.

## 영구 배포와 복구

`/opt/docmind-avdc/config/compose.json`의 `docmind` 서비스에 다음 읽기 전용 바인드 마운트를 추가했다.

| 호스트 파일 | 컨테이너 파일 |
|---|---|
| `runtime/api/office_chunker.py` | `/ragflow/rag/parser_platform/office_chunker.py` |
| `runtime/api/parser_config.py` | `/ragflow/rag/parser_platform/config.py` |

동일 서비스의 `PARSER_PLATFORM_KORDOC_CHUNKER_REVISION`은 `docmind-kordoc-chunker-v3`이다. 기존 구성과 대조하여 두 마운트와 이 변수 외의 서비스·자원·네트워크·볼륨 설정이 동일함을 확인했다. `manage.sh up -d --no-deps --force-recreate docmind`로 API만 교체했다. 나머지 컨테이너 ID는 모두 유지됐다.

기존 Compose 및 두 코드 파일은 AVDC의 `management/chunking-20261110` 아래에 보관했다. 롤백 시 `compose-before.json`을 원래 Compose 경로로 복원하고 `docmind` 서비스만 다시 생성하면 이전 이미지 내 청킹 코드로 돌아간다. 비밀값이 포함될 수 있는 실제 Compose와 백업은 Git에 넣지 않았다.

실제 배포 파일 SHA-256:

- 청커: `5eccc80e948274aeaaafac0489a7ba22d47bdffa7f0c457cd1034547faac7f13`
- 설정 모듈: `d6e410e320f2e8d253974e423d4008c7e9062b4bd6dc265c6a81b8b4168f4b77`

## 검증과 적용 범위

- 파서/청커 회귀 및 버전 fingerprint 검사: **75개 통과**. 제거된 제품 런타임 참조 검사 통과.
- 적용 후 API healthy, Elasticsearch green. 두 코드 파일 해시와 읽기 전용 마운트, 실제 로딩된 청킹 버전 v3 확인.
- 운영 컨테이너의 실제 청커에 보관한 같은 파서 출력을 넣어 **440개 / 48토큰 미만 44개 / 표 108개**가 재현되고, 비교 UI 후보와 청크 본문 전체의 해시가 일치함을 확인했다. 이 검증의 색인 쓰기는 0회이며 임시 입력을 정리했다.
- 기존 운영 색인 항목 **45,636개**, 사례집 활성 청크 **794개** 및 활성 청크 세트 동일.
- 사전 동일 8문항 비교: 기존·2차 후보 모두 정답 근거 1위 **8/8**. 단일 사례집을 선택한 검색 검증이며 전체 자료의 정확도를 보장하는 수치는 아니다.
- 새 규칙은 이후 새로 색인하거나 실제 재색인하는 문서부터 사용한다. 기존 794개 청크를 440개로 교체하거나 전체 문서를 재색인하지 않았다.

비교 수치와 세부 검색 순위는 [비교 기록](PDF-SHORT-HEADING-DOWNSTREAM-V2-2026-11-06.json), 디스크 정리 내역은 [복구 기록](AVDC-DISK-RECOVERY-2026-11-10.md)을 참조한다.
