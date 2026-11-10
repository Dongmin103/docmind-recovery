# OpenVINO 설치 패키지 — 색인 검증 미완료 후보본

2026-11-10. 기존 AVDC 운영은 변경하지 않았다. 사용자가 디스크 공간을 이유로 추가 검증을 중단하고 준비된 이미지와 보고서를 우선 게시하도록 요청했다.

## 내려받기

- [GitLab 릴리스](http://128.134.187.123:9010/rlaxortj00/docmind/-/releases/v2026.11.10-openvino.1)
- [패키지 목록](http://128.134.187.123:9010/rlaxortj00/docmind/-/packages)
- [최신 통합 보고서](reports/DOCMIND-INTEGRATED-2026-11-10.pdf)

릴리스의 `download.py`를 받고 Linux/Windows의 Python으로 실행한다. GitLab 읽기 권한 토큰은 숨김 입력하며 파일에 저장하지 않는다.

```bash
python3 download.py --base-url http://128.134.187.123:9010/api/v4/projects/181/packages/generic/docmind-openvino/2026.11.10-r1/
```

ZIP은 6,810,387,148바이트이며 64MiB 이하 102개 분할 파일로 전송한다. 다운로드 도구는 부분 파일을 재사용하고 SHA256을 대조한 뒤 ZIP을 합친다. 완성 ZIP의 SHA256은 `6f73028c7e718da9cc3f2d29c52b7abc922a52b744f19e09e7d4522fd568aad1`이다. 재다운로드 없이 저장된 부분 파일로 재개할 수 있으나 부분 파일과 ZIP을 합칠 공간은 필요하다.

## 포함 범위

API·최신 청킹 v3·Kordoc 파서·OpenVINO FP32 모델 및 토크나이저·DB/검색 저장소 등 이미지 8개, 설치 도구, Windows 워커 소스·설정 생성 도구, 라이선스와 해시 목록이다. 이미지 이름과 ID, 포함 소스 commit은 ZIP의 `manifest.json`에 기록했다. 운영 원본·색인·키·상용 복호화 EXE/DLL은 제외했다.

설치는 [설치 안내](../deploy/openvino/README.md)를 따른다. Ubuntu Linux amd64, Python, Docker Engine/Compose는 사전 설치해야 한다. `compact`는 소규모 확인용이고 `avdc`는 96개 논리 프로세서 환경용이다. 기존 AVDC에 덮어쓰는 업데이트 패키지가 아니다.

## 확인 결과와 남은 작업

- 별도 Docker daemon 저장소와 containerd namespace에서 이미지·컨테이너·볼륨 0개로 시작했다. 기존 WSL Ubuntu를 사용했으므로 새 OS 설치 검증은 아니다.
- 이미지 로드와 ID 대조, 서비스 8개 기동, 모델 2개·source 0개·768차원 초기화를 확인했다.
- 실제 색인 검증은 **미완료**다. `install.py check`가 합성 PDF를 만들 때 API 이미지에 없는 `fitz`를 불러와 중단됐다. 실제 문서 파싱·임베딩에 진입하기 전이며, 이 오류만으로 엔진의 성공·실패를 판단할 수 없다.
- 남은 작업은 검증 스크립트의 PDF 생성 의존성 제거/수정, 합성 DOCX·PDF 색인 및 활성 청크·벡터·정리 ACK 확인, 내려받은 패키지의 신규 설치 시험이다. 현재 ZIP의 `check` 명령은 위 오류가 알려져 있다.
- 외부 KURE, 답변 모델, 상용 복호화 모듈, Windows 파일 감시, 사용자 권한 연동은 이번 빈 환경 검증 범위에 포함되지 않는다.
- 기존 운영에서 측정한 성능·검색 품질 결과는 통합 보고서에 보존했다. 그 결과를 이 신규 설치본의 검증 완료로 확대하지 않는다.

GitLab 전송 무결성 확인은 서버가 계산한 각 파일 SHA256 대조로 수행한다. 전체 다운로드 후 설치·색인 합격과는 별개다.
