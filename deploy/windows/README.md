# DOCMIND Windows 실행 패키지

Docker 이미지·GTE 모델·파서·OCR 모델·실행 스크립트를 포함한 패키지입니다.
대상 서버에서 Git clone, npm/pip 설치나 이미지 빌드는 필요하지 않습니다.
문서와 DB, 현재 서버의 인증 키는 포함하지 않습니다.

## 필요한 서버 환경

- Windows 10/11 또는 호환되는 Windows 서버 환경에서 WSL2 Linux 컨테이너를
  실행할 수 있는 Docker Engine 29.1 이상·Compose v2.30 이상을 준비합니다.
  포함 이미지는 Linux/amd64이며 Docker Engine 29.1.3에서 저장/로드를 검증합니다.
- 분할 다운로드·ZIP 복원·압축 해제·Docker 이미지 로드를 위해 설치 전 여유
  디스크 120GB를 권장합니다. 문서 색인이 커지는 데 필요한 공간은 별도입니다.
- CPU 버전은 RAM 16GB 기준으로 문서·임베딩을 순차 실행합니다. 기본 시작은
  일반 문서 처리·검색·답변이며, OCR은 추가 메모리를 쓰므로 필요할 때 켭니다.
- GPU 버전은 NVIDIA GPU와 호환 Windows 드라이버, Docker GPU 지원이 필요합니다.
  GTE는 FP32 CUDA 12.6/PyTorch 2.8.0을 사용하며 지원되지 않는 GPU에서 CPU로
  자동 전환하지 않습니다. 드라이버 설치 파일은 포함하지 않습니다.
  GPU 버전은 시작 시 OCR도 기동합니다.
  현재 배포본의 문서 슬롯 4·파서 2·임베딩 배치 32/요청 동시성 2를 유지합니다.
- 암호화 원본을 처리하려면 서버에 설치된, 서명이 검증되는 uEncryptor2를 사용합니다.
  임시 평문 작업 볼륨의 BitLocker와 백업 제외 조건은 호스트 워커가 검사합니다.
- Jina v3.5 리랭커 API와 기존 Qwen 답변·검토 서버를 연결합니다.
  Jina 키 파일과 Qwen 서버의 `AnswerBaseUrl`이 필요합니다. 답변은 현재와 같은
  `reviewed_v1`, qwen3.8-flash-next, 문맥 32768/출력 4096 설정을 유지합니다.
  답변 서버 연결이 필요합니다. 이미지·로컬 임베딩·파서 자원은 추가 다운로드 없이 로드합니다.

## 첫 실행

1. CPU 또는 GPU ZIP을 새 폴더에 풉니다. GitHub의 파일당 용량 제한으로
   `.zip.part001` 등으로 나뉜 경우, 원하는 버전의 모든 part와 해당
   `*.parts.json`, `Join-DocMindPackage.ps1`을 같은 폴더에 받은 뒤 아래 명령으로
   해시를 확인하며 하나의 ZIP으로 합칩니다.
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\Join-DocMindPackage.ps1 -ManifestPath .\docmind-windows-cpu.parts.json`
   GPU는 파일명의 `cpu`를 `gpu`로 바꿉니다. 함께 제공된 `.sha256`으로 ZIP 해시를
   확인할 수 있습니다. 실행 스크립트도 이미지와 모델 파일의 해시를 확인합니다.
2. `server-settings.example.json`을 Git 밖의 실제 서버 설정 파일로 복사합니다.
   Jina API 키 파일, 기존 Qwen 서버 주소, 이미 설치된 uEncryptor2, 임시 작업 폴더와 원본 루트를
   지정합니다. 다른 문서 집합에는 새 `source_id`를 사용합니다. API 키 파일에는
   키 한 줄만 저장하며, 설정 JSON에 키 자체를 적지 않습니다.
3. 이 패키지 폴더의 PowerShell에서 실행합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Initialize-DocMind.ps1 -SettingsPath C:\DocMindHost\server-settings.private.json
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-DocMind.ps1 -StartWorkers
```

최초 설정은 새 DB 암호·세션 키·호스트 인증 키를 만듭니다. 시작 시 포함된
이미지를 Docker에 로드하고 빈 서비스·모델 등록·작업공간·논리 원본 등록을
초기화합니다. 이미 있는 설치의 키를 덮어쓰지 않습니다. 원본을 아직 등록하지
않고 앱만 기동할 때는 설정의 `Sources`를 `[]`로 지정하고 `-StartWorkers`를
생략합니다. 이후 원본 추가는 이 설치의 관리 구성과 호스트 설정을 함께 맞춥니다.
GTE와 OCR 모델은 이미지에 내장되어 Docker 파일시스템에서 읽습니다.
Windows 공유 폴더의 대용량 모델 파일을 직접 읽거나 별도로 내려받을 필요가 없습니다.

기본 화면은 `http://127.0.0.1:18080`입니다. 기본 API는 `127.0.0.1:19380`에만
노출합니다. 여러 서버나 외부 사용자의 접속 주소·TLS·인증 경계는 대상 환경에서
구성해야 합니다. 패키지는 현재 공용 작업공간 동작을 유지합니다.

## OCR과 중지

```powershell
# CPU에서는 일반 검색에 필요할 때만 OCR 서비스를 추가 기동합니다.
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-DocMind.ps1 -WithOcr

# 호스트 워커의 현재 작업·정리가 끝난 뒤 컨테이너를 멈춥니다.
powershell -NoProfile -ExecutionPolicy Bypass -File .\Stop-DocMind.ps1
```

OCR은 화면에서 요청한 검색 불가 PDF에만 적용합니다. CPU에서도 같은 Surya
모델을 GPU 레이어 0으로 실행하며, GPU 버전은 실제 GPU 실행 확인을 유지합니다.
CPU OCR의 속도는 GPU와 같지 않습니다. 긴 페이지는 기존 요청 시간 제한으로
명시적으로 실패할 수 있습니다. 워커는 현재 Windows 사용자 세션에서 동작하며,
로그아웃·재부팅 후 무인 실행을 이 스크립트만으로 보장하지 않습니다.

`.local/`에는 실제 키·서버 설정·호스트 동기화 상태가 생깁니다. 외부에 공유하거나
다른 서버의 설정으로 복제하지 마세요. 중지 명령은 Docker 볼륨을 삭제하지 않습니다.

배포 파일 구성·이미지 ID·소스 커밋은 `runtime-manifest.json`, 라이선스와 모델
고지는 `licenses/`에 있습니다. 검색은 C128·문서당 8·본문 prefix2400·Jina v3.5·Top5,
임베딩은 GTE multilingual base 768차원 계약을 유지합니다.
