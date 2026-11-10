# GitLab·GitHub main 동기화 기록

사용자 요청 범위는 GitLab의 기존 main 보관, 최신 AVDC 코드·오늘 154개 측정 결과·보고서 반영, GitHub main 동기화다. 서버 재배포나 새 실행 ZIP 발행은 포함하지 않는다.

## 보관 브랜치

| GitLab 브랜치 | 보관하는 커밋 |
|---|---|
| `legacy` | 동기화 직전 main `6bbbb1ee5b12f3adfc4f3e1d7639d66d73854a96` |
| `legacy-before-20261110` | 원래 legacy `be7de13c115f9be0ca4bb661b01383f033e077b5` |

두 보관 브랜치를 먼저 원자적으로 반영하고 원격 SHA를 대조했다. 기존 `handoff/research-20261102` 브랜치는 유지한다. main 교체는 확인한 기존 SHA에 대한 `force-with-lease`로 한정하며, 보호 브랜치의 강제 푸시 허용을 일시 변경한 후 원래 설정으로 되돌린다. GitHub main은 일반 fast-forward 푸시를 사용한다.

## 함께 반영한 내용

- GitHub에 있던 OpenVINO 운영 소스·자원 배분 예제·병렬 수집, PDF 청킹 v3, 디스크 정리·확장 기록.
- GitLab에만 있던 `74e99c9`의 Windows 설치 패키지 KURE Nano 기본값 변경. 원저자·내용을 보존한 cherry-pick으로 통합했다.
- [154개 초기화 후 재색인](AVDC-INDEXING-154-2026-11-10.md)과 [문서 정보 없는 집계 JSON](measurements/avdc-fresh154-2026-11-10.json).
- [최신 종합보고서](reports/AVDC-STATUS-2026-11-10.md), [7쪽 PDF](reports/AVDC-STATUS-2026-11-10.pdf), 재생성 스크립트 `tools/reports/build_avdc_status_20261110.py`.
- README·현재 구조 문서·OpenVINO 전환 기록에 최신 상태 연결. 과거 535건·689건 등의 기록은 당시 이력이며 현재 활성 문서 수는 152개다.

GitLab의 조회한 브랜치 파일·위키·릴리스·이슈에서는 기존 PDF 보고서를 찾지 못했다. 첨부 목록 API는 지원되지 않아 저장소 밖의 모든 첨부 존재까지 확인한 것은 아니다. 보관 중인 11월 6일 보고서와 원본 집계 기록을 기준으로 최신 종합본을 작성했다.

## 검증 및 제외 범위

- 패키징 경계·손상 검사 4건, AVDC OpenVINO 배포 회귀 8건 통과.
- Windows 초기화 PowerShell 구문, Python 초기화·패키저 구문, KURE 모델·키 파일 설정 일치 확인.
- PDF 7쪽 렌더 확인, 핵심 수치·Markdown 링크·추가 파일 비밀값 패턴 검사.
- 기존 미커밋 CPU 배치 실험 2개 파일(`tools/local_embedding/service.py`, `test_service.py`)은 이 반영 대상이 아니며 작업 상태를 유지한다.
- API 키·개인키·원본·문서별 로그·청크·벡터·DB 백업·실제 운영 설정은 추가하지 않는다. 집계 값과 모델/배포 예제만 반영한다.
- 양쪽 main과 로컬 HEAD가 같은 SHA인지, GitLab 보관 브랜치 SHA와 main 보호 설정이 유지됐는지 원격 조회로 확인한다.
