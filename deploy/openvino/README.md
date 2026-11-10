# DocMind OpenVINO 설치본

Ubuntu의 Docker에서 실행하는 CPU 전용 엔진입니다. 최신 청킹 v3와 OpenVINO FP32 모델이 이미지에 포함됩니다. 모델 다운로드·이미지 빌드·OCR·GPU 없이 설치합니다. 원본, 색인, 운영 키는 포함하지 않습니다. Windows 암호화 원본은 별도의 Windows 워커와 설치된 uEncryptor2를 연결합니다.

## 사전 조건

- Ubuntu Linux amd64, Python 3.10 이상, Docker Engine 및 Compose v2. Docker 명령 실행 권한.
- 소형 설정 `compact`: 논리 프로세서 6개 이상, Docker 메모리 7GiB 이상. 임베딩 1작업자 × 4스레드, 문서 1슬롯, 파서 1개·2 CPU 한도. 성능 실측의 AVDC 설정과 다릅니다.
- AVDC 설정 `avdc`: CPU 번호 0~95, Docker 메모리 60GiB 이상(권장 64GiB 이상). 임베딩 22작업자 × 4스레드·한도88, 파서2개·한도4, 문서12슬롯. 서버 사양과 기존 업무 여유를 먼저 확인합니다.
- 이미지 저장소와 설치 경로에 충분한 공간. ZIP·이미지 로드·DB를 합해 처음에는 50GiB 이상 여유를 권장합니다. 문서량에 따른 색인 공간은 별도입니다.
- 이 설치본은 새 서버/새 프로젝트용입니다. 기존 AVDC에 덮어쓰거나 기존 DB 볼륨에 연결하지 않습니다.

## 다운로드와 설치

GitLab 릴리스에서 `download.py`를 받고 릴리스에 적힌 패키지 URL로 실행합니다. 읽기 권한 토큰은 숨김 입력하며 파일에 저장하지 않습니다. 분할 파일과 완성 ZIP의 SHA256을 검사합니다.

```bash
python3 download.py --base-url <릴리스에_기재된_Generic_Package_URL>
python3 -m zipfile -e docmind-download/docmind-openvino-2026.11.10-r1.zip /새로운/설치경로
cd /새로운/설치경로/docmind-openvino
python3 install.py verify
python3 install.py install --profile compact --project docmind-ov-main
python3 install.py check
```

AVDC와 같은 자원을 확보한 새 서버는 설치 명령의 `--profile avdc`를 선택합니다. 검증/다운로드 PC와 대상 VM이 다르면 완성 ZIP을 옮겨 설치하면 됩니다. 대상 VM의 인터넷 연결이나 NAT 생성은 필요 없습니다.

설치 시 Docker 이미지를 불러오고 새 DB·빈 볼륨·인증키를 만듭니다. `.local/`에는 서버별 키와 설정이 생기므로 Git이나 배포 ZIP에 다시 넣지 않습니다. 동일 폴더 재설치 및 이미 사용 중인 프로젝트명은 거부합니다. 실패 후 `install`을 반복하지 말고 `status`와 로그를 확인한 다음 `start`로 재개합니다.

기본 UI `http://127.0.0.1:18081/docmind?view=library`, API `http://127.0.0.1:19391`. 다른 PC에서 볼 때는 승인된 SSH 터널/리버스 프록시를 따로 구성합니다. 이 도구는 방화벽·NAT·기존 서비스 설정을 변경하지 않습니다.

```bash
python3 install.py status
python3 install.py stop      # 데이터는 보존
python3 install.py start
docker compose -f .local/compose.json logs --tail 80 docmind
```

## 설치 확인의 범위

`check`는 새 합성 DOCX와 PDF를 만들어 전용 논리 source에 등록합니다. HMAC 인증 요청으로 관찰→임대→임시 입력 전달→실제 Kordoc 파싱→청킹 v3→OpenVINO 768차원 벡터→Elasticsearch 활성화→임시 파일 삭제·정리 ACK를 실행합니다. 완료 상태, 활성 버전, 청크 수, 검색 저장소의 표식/벡터를 확인하고 결과를 `PACKAGE_ACCEPTANCE=` JSON으로 출력합니다. 시험 청크는 UI 확인을 위해 남습니다. 다시 실행하면 새 시험 source가 만들어집니다.

이는 **평문 전달 이후의 엔진 검증**입니다. 상용 복호화 EXE 실행, Windows 파일 감시, 사용자 ACL, 외부 KURE 재평가와 답변 모델 호출은 이 검증에 포함되지 않습니다. 외부 키가 없어도 색인·자료/청크 확인은 가능합니다. 검색/답변 전체 운영은 별도 연동 후 검증합니다.

## 실제 Windows 원본 연결

1. Ubuntu `.local/secrets/worker-key`를 승인된 방법으로 Windows의 제한된 위치에 전달합니다. 재발급하면 API와 워커가 서로 달라지므로 동일 키를 사용합니다.
2. `tools/handoff/engine.py projects`로 프로젝트를 확인하고 `register-source --project <ID> --source <source-id> --display-name <표시명> --apply`로 원본 등록을 합니다. 도구는 API 컨테이너에서 `/ragflow/.venv/bin/python /opt/docmind/handoff/engine.py ...`로 실행합니다.
3. Windows에 설치된 uEncryptor2와 DLL, 승인된 실행 파일 해시/서명, 원본 폴더, 전용 임시 평문 폴더의 백업 제외·보호 상태를 확인합니다. `configure-worker.ps1`의 필수 인자로 경로를 넣어 설정을 생성합니다. 기존 운영 워커와 중복 감시하지 않습니다.
4. `tools/windows/Watch-DocMindEncryptedSources.ps1 -ConfigPath <worker.json>`와 `Start-DocMindIngestionPool.ps1 -ConfigPath <worker.json> -Concurrency 1 -StopFile <전용중지파일경로>`을 실행합니다. AVDC 자원 프로필에서 검증 후 최대 12슬롯으로 확장합니다. 전체 인자는 각 스크립트 `Get-Help` 및 포함된 `docs/engine-handoff`를 따릅니다.

복호화 모듈과 실제 인증키는 이 설치본에 포함되지 않습니다. 기존 GPU 설치본은 이 CPU 설치본과 별도입니다.
