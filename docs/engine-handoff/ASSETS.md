# 실행 자산 목록과 전달 방법

2026-11-10 AVDC에서 읽기 전용으로 조회했다. 정확한 운영 이미지 ID, 자원 한도, 모델·핵심 소스 SHA256은 [assets.json](assets.json)에 있다. 아래 운영 자산 목록과 새 설치 패키지는 구분한다.

## 새 OpenVINO 설치 패키지 상태

최신 청킹 v3와 OpenVINO FP32 모델을 포함한 Linux amd64 이미지 8개를 새 설치 패키지로 만들었다. ZIP은 6,810,387,148바이트(약 6.81GB)이며, GitLab Generic Package Registry의 `docmind-openvino / 2026.11.10-r1`과 연결 릴리스에서 배포한다. 이미지·모델은 Git 이력에 넣지 않고 패키지 자산으로 전달한다. [릴리스 안내](../OPENVINO-PACKAGE-2026-11-10.md)를 우선 확인한다.

| 항목 | 확인 상태 |
|---|---|
| 분리된 빈 Docker 환경 | 기존 WSL Ubuntu의 별도 Docker 저장소·namespace, 시작 시 이미지·컨테이너·볼륨 0개 |
| 이미지 로드·기동 | 이미지 ID 대조, 8개 서비스 기동 및 초기화 완료. 새 OS 설치 시험은 아님 |
| 실제 색인 | 이번 패키지 게시에서는 완료 확인 범위에 포함하지 않음 |
| GitLab 다운로드 후 재설치 | 미수행. 사용자가 디스크 공간을 이유로 추가 검증보다 우선 업로드하도록 변경 |
| 기존 환경 | AVDC 운영 변경 없음. 로컬 검증 컨테이너는 중지하고 기존 GPU 서비스는 유지 |

이번 패키지에는 운영 원본·청크·색인·실제 키를 넣지 않았다. 상용 Windows 복호화 모듈은 별도 수령하며, KURE·답변 모델 호출도 별도 연동 대상이다. 패키지 안의 인수인계 문서는 빌드 시점 스냅샷이므로 최신 상태는 릴리스와 GitLab main의 보고서를 따른다.

## 1. Git으로 받는 것

GitLab `rlaxortj00/docmind`의 main을 받고 실제 받은 `git rev-parse HEAD`를 인수 기록에 적는다. GitLab 접근 토큰은 승인된 credential helper로 제공하며 URL에 넣지 않는다.

```bash
git clone --branch main http://128.134.187.123:9010/rlaxortj00/docmind.git
cd docmind
git rev-parse HEAD
```

- API·청커·파서 연결·Windows 워커·OpenVINO 풀 소스.
- 설정 예제, [연동 명세](API.md), [실행/합격 절차](RUNBOOK.md), 합성 문서 생성·상태 확인 도구.
- 모델 변환 코드 및 버전 고정 기록. 모델 바이너리·컨테이너 이미지 자체는 별도다.

## 2. 별도로 전달할 것

| 자산 | 현재 위치/식별자 | 수령 확인 |
|---|---|---|
| API 이미지 | AVDC Docker `docmind-avdc:cpu-noocr-20261102` | 이미지 ID `6efabc34…`, 전체 값 assets.json |
| OpenVINO 이미지 | `docmind-gte-openvino:20261106` | 이미지 ID `5b54a0e5…` |
| 파서 이미지 | `docmind-kordoc-parser:2026.10.19` | 이미지 ID `53fc3d95…` |
| ES·MySQL·Valkey·MinIO·preview 이미지 | 각각 assets.json의 `configured_image` | tag만이 아니라 ID 대조 |
| API overlay 소스 | `/opt/docmind-avdc/runtime/api` | **기반 API 이미지와 함께 필요**. 이미지 이름 날짜만으로 최신 청킹 포함 여부 판단 금지 |
| 임베딩 소스 | `/opt/docmind-avdc/runtime/embedding` | service/pool SHA256 대조 |
| OpenVINO 모델 | `/opt/docmind-avdc/models/openvino/model.xml` 및 `model.bin` | 두 파일 모두 assets.json SHA256 일치, bin 약 1.26GB |
| 토크나이저·원모델·신뢰 코드 캐시 | 임베딩 기반 이미지의 `/opt/docmind/model` 및 모델 캐시 | 모델 revision `9bbca17d9273fd0d03d5725c7a4b0f6b45142062`, 코드 revision `40ced75c3017eb27626c9d4ea981bde21a2662f4` |
| 실제 Compose·설정 | `/opt/docmind-avdc/config`, `manage.sh` | 환경별 경로·마운트·이미지·포트 대조. 실제 설정은 별도 비공개 전달 |
| 키 | Ubuntu secrets 및 Windows 워커 설정이 가리키는 키 | HMAC·임베딩·세션·DB 키를 용도별 전달, Git 업로드 금지 |
| Windows 복호화 모듈 | AVDC `D:\DocMind\worker\decryptor` | exe와 의존 DLL을 함께, 공급사 배포 조건 준수 |
| 워커 실제 설정·코드 | `D:\DocMind\worker\runtime`, `scripts` | 원본 루트·API loopback·공유 복호화 게이트·실행 계정 확인 |

기존 uEncryptor2의 승인 기록은 [복호화 검증](../UENCRYPTOR2-VALIDATION.md)의 버전 1.0.0.2, SHA256 `8f1506520127011b0c425121fa583bcc9c1533b60ca9cd28540d45b5c645d457`이다. **이번에는 Windows 바이너리를 재해시하지 않았다.** 인수 시 실제 exe의 해시·Authenticode를 확인한다. DLL 해시와 배포 권한도 별도 인수 목록에 적는다.

현재 모델/이미지를 그대로 전달하는 경로와 새로 빌드하는 경로를 구분한다. OpenVINO [Dockerfile/변환 안내](../../tools/avdc_embedding/README.md)는 기존 기반 이미지와 오프라인 wheels를 전제로 한다. 빈 서버에서 전체 빌드를 검증한 새 릴리스가 아니다.

## 3. 이미지·파일 전달을 준비할 때

아래는 기존 운영 자산을 별도로 인수할 때의 **선택적 내보내기 절차**다. 새 설치 패키지를 받는 경우 다시 내보낼 필요가 없다. 이번 패키징에서는 모델과 기반 이미지를 읽기 전용으로 가져왔으며 AVDC 서비스 재기동은 하지 않았다. 새 디렉터리를 쓰고 디스크 여유를 먼저 확인한다. Compose의 실제 이미지 이름이 assets.json과 다른 경우 먼저 원인을 확인한다.

```bash
# Ubuntu AVDC, 기존 관리 폴더 권한을 가진 계정
sudo /opt/docmind-avdc/manage.sh images
df -h /opt/docmind-avdc
```

승인된 전달 디렉터리에서 Docker save로 다음 **8개 이미지**를 하나의 archive로 내보낸다. 이 명령은 실행 중인 컨테이너를 중지하지 않지만 디스크/IO를 사용한다.

```bash
# /secure-transfer는 담당자가 준비한 전용 경로로 바꾼다.
# 기존 결과를 덮어쓰지 않도록 존재 여부를 확인한다.
test ! -e /secure-transfer/docmind-images.tar || exit 1
sudo docker save -o /secure-transfer/docmind-images.tar \
  docmind-avdc:cpu-noocr-20261102 docmind-gte-openvino:20261106 \
  docmind-kordoc-parser:2026.10.19 docmind-preview:2026.10.19 \
  elasticsearch:8.11.3 mysql:8.0.40 valkey/valkey:8 \
  pgsty/silo:RELEASE.2026-08-06T00-00-00Z
sha256sum /secure-transfer/docmind-images.tar
```

출력 tar의 SHA256은 **새로 생성한 파일의 전송 확인용**이며 assets.json의 Docker image ID와 다른 값이다. runtime 코드·OpenVINO 모델·설정·키는 별도 전달한다. 특히 `/opt/docmind-avdc` 전체를 무작정 압축하면 운영 DB·원문 파생물·키까지 포함될 수 있다. 필요한 디렉터리만 선별한다.

수신 서버에서 archive SHA256을 송신 측과 대조한 뒤 `docker load -i <전달받은 tar>`를 실행하고 `docker image inspect <tag> --format '{{.Id}}'`를 assets.json과 비교한다. 모델은 `sha256sum model.xml model.bin`, Windows 파일은 `Get-FileHash -Algorithm SHA256`으로 확인한다.

## 4. 새 서버와 기존 AVDC의 차이

- **기존 AVDC 인수:** 현재 이미지·마운트·프로젝트가 준비돼 있다. [RUNBOOK](RUNBOOK.md)의 읽기 확인 및 새 시험 source 등록부터 진행한다.
- **새 서버 설치:** 위 자산과 서버별 설정·빈 인프라·마운트 경로를 먼저 준비한다. 현재 AVDC Compose는 민감 설정을 포함하므로 통째로 공개하거나 다른 서버에서 바로 실행하지 않는다.
- 기존 Windows 패키지의 `deploy/windows/scripts/bootstrap.py`는 모델·공유 작업공간·source 초기화를 수행한다. `Initialize-DocMind.ps1`/`Start-DocMind.ps1`과 묶인 **해당 패키지의 새 설치**에 사용한다. 이미 운영 중인 AVDC에 임의 실행하면 tenant 모델 기본값까지 바뀔 수 있다.
- 새 OpenVINO 설치 ZIP을 제공한다. Windows 상용 모듈 전달과 신규 서버 전체 연동 시험은 별도 항목이다.
