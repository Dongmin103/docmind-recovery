# Windows 서버에서 개발 시작

이 브랜치는 **소스와 설계를 받아 개발을 시작하는 기준점**이다. 완성된 OpenViking 제거판이나 Windows에서 기동 검증된 배포판이 아니다. 원본 PDF·운영 청크·DB·MinIO 데이터를 이동하지 않는다.

## 받기

Windows PowerShell에서 Git이 설치된 상태로 실행한다. 대상 폴더는 새 개발용 경로를 사용한다.

```powershell
git clone --branch windows-development-handoff --single-branch https://github.com/Dongmin103/docmind-recovery.git C:\DocMindDev\docmind
Set-Location C:\DocMindDev\docmind
Get-Content .\START_HERE.md
powershell -NoProfile -File .\tools\windows\Test-DevelopmentEnvironment.ps1
```

소스는 app/, 요구사항은 루트 PRD.md, 구조 설명은 docs/CURRENT-ARCHITECTURE.md에 있다. ZIP 다운로드도 가능하지만 변경 이력을 남기려면 git clone을 사용한다. 기존 recovery release와 그 복호화 키는 이 작업에 필요하지 않다.

## 서버 쪽 Codex에게 그대로 전달

> 이 저장소의 AGENTS.md, PRD.md, WINDOWS_DEVELOPMENT.md, docs/CURRENT-ARCHITECTURE.md, docs/HANDOFF-STATUS.md를 읽고 Windows Docker 기반 DocMind 리팩터링을 진행해 주세요. app/가 기존 커스텀 소스입니다. 이전 서버의 PDF·청크·DB·MinIO 데이터는 가져오지 않습니다. 먼저 현재 Windows에서 Docker Linux 컨테이너 실행 가능 여부와 기존 클라우드/uEncryptor2 설치를 읽기 전용으로 확인하고, 독립된 개발 폴더와 빈 개발 DB/볼륨을 준비하세요. 비민감 합성 표본으로 자동 복호화 실행 조건과 요청 시 원문 미리보기를 검증하면서, OpenViking 제거·구성 C128/prefix2400/Jina v3.5·source/폴더/문서 범위 검색·동기화를 구현하세요. 대화 기록은 사용자별 30일 조회/검색을 지원합니다. 문서 쓰기는 실제 클라우드 연계가 검증된 기능부터 공개합니다. 시험 규모는 나중에 제공하므로 다른 개발을 멈추지 마세요. 기존 클라우드 원본이나 서비스는 변경하지 말고, 비밀값은 Git에 넣지 마세요. 변경과 테스트 결과를 기능별 커밋으로 남겨 주세요.

## 첫 작업 순서

1. 사전 점검 스크립트 결과로 Windows 버전, Git, Docker/Compose, Linux 컨테이너 모드, 자원과 세 루트의 존재 여부를 확인한다. 스크립트는 설치·복호화·서비스 변경을 하지 않는다.
2. 기존 uEncryptor2의 위치/버전/의존·실행 계정을 확인한다. exe 및 관련 상용 모듈은 이 저장소에 포함하지 않는다. 대상 서버의 설치를 사용하고 원본 저장 루트 밖의 비민감 표본으로만 시험한다.
3. app/docker/의 기존 Compose와 env 예제를 검토한다. Linux 호스트 절대 경로·고정 container_name·포트·OpenViking 의존·공용 로그인 설정이 남아 있다. **그대로 compose up 하지 않는다.** 개발 전용 프로젝트명/볼륨, Windows 경로 및 호스트 통신, loopback 포트, 새 자격증명을 준비한다.
4. 새 모델 파일은 필요에 따라 다운로드한다. BGE-M3 tokenizer 소스 자원은 포함하지만 모델 가중치·Docker 이미지 캐시는 없다. 다운로드·이미지 빌드 시간이 필요하다.
5. PRD 순서대로 검색과 수집/미리보기 연계를 독립적으로 구현한다. 기존 원본을 새 Windows로 옮기지 않고 합성 문서 및 사용자가 지정할 시험 문서를 사용한다.

## 설정과 의존성

- Python 백엔드는 3.13+, web은 Node.js 22와 package-lock/uv.lock을 기준으로 한다. 네이티브 의존이 있어 백엔드 설치/실행은 Docker의 Linux 개발 환경에서 진행한다.
- 실제 docker/.env, docker/.env.docmind.cpu, conf/service_conf.yaml, 개인키는 포함하지 않았다. 예제와 docker/service_conf.yaml.template를 바탕으로 **새 개발용 값**을 만든다. Jina/답변 모델의 API 키도 서버에서 별도로 설정한다.
- conf/system_settings.json은 외부 연동 값을 비운 초기 설정이다. 초기 계정 등록/관리자 생성 정책은 소스를 확인해서 준비한다.
- 로그인 암호화용 conf/private.pem과 public.pem도 제외했다. 프런트에 기존 공개키가 고정되어 있으므로 새 키 쌍과 함께 갱신해야 한다. Python 의존성 준비 후 아래 도구를 개발 컨테이너에서 실행한다. 이 키는 uEncryptor2 복호화 키나 운영 백업 복호화 키와 무관하다.

```bash
# 저장소 루트, 프로젝트 의존성(pycryptodomex)이 준비된 Python 환경
python tools/windows/initialize_auth_keys.py
python tools/windows/initialize_auth_keys.py --check
```

도구는 기존 키를 덮어쓰지 않고 새 개인키는 ignored 경로에만 생성한다. 변경된 프런트 공개키로 웹을 다시 빌드해야 한다. 기존 암호화 비밀번호 테스트 상수는 제외했으므로 해당 통합 테스트는 로컬 키로 만든 DOCMIND_TEST_ENCRYPTED_PASSWORD를 사용한다.

## 검증

- 백엔드: 실제 변경 부위의 pytest/ruff. 전체 환경이 갖춰지지 않은 상태에서 AST 검사를 서비스 기동 성공으로 보고하지 않는다.
- 프런트: app/web에서 npm ci, npm run type-check, npm run build와 관련 테스트. Linux 개발 컨테이너 실행을 우선한다.
- Go: 변경이 필요한 경우 app/AGENTS.md에 따라 build.sh --test를 사용한다.
- 합성 fixture는 코드 테스트용으로 남아 있지만 PDF/Office 원본 파일을 읽는 기존 테스트 일부는 별도 fixture가 필요하다. 사용자 원문을 받아 이 부족분을 채우지 말고 합성 fixture를 생성한다.
- 최종 Windows Docker 기동·uEncryptor2·실제 API·성능 검증은 이 서버에서 수행할 후속 작업이다.
