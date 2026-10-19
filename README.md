# DOCMIND

등록한 Windows 원본 폴더의 문서를 색인하고, 전체·폴더·문서 범위로 검색하며 근거가 있는 AI 답변을 제공합니다.

## 설치와 실행

배포 브랜치는 `main`입니다.

```powershell
git clone --branch main --single-branch https://github.com/Dongmin103/docmind-recovery.git docmind
cd docmind
```

1. [서버 배포 안내](docs/SERVER-DEPLOYMENT-2026-10-19.md)에 따라 Docker, 파서, GTE, 모델 인증과 서버별 설정을 준비합니다.
2. [원본 폴더 등록 절차](docs/SERVER-DEPLOYMENT-2026-10-19.md#새-서버에서-다른-원본-폴더를-색인하기)에 따라 대상 서버의 원본과 실제 경로를 연결합니다.
3. DOCMIND 이미지를 빌드하고 서버 설정으로 실행한 뒤, 감시기와 색인 워커를 시작합니다.

```powershell
docker build -f app/Dockerfile -t docmind:2026.10.19 ./app
```

소스·환경설정 예제와 실행 도구를 포함합니다. Docker 이미지·모델 가중치·uEncryptor2·실제 인증 키·문서·DB·색인 데이터는 Git에 포함하지 않습니다. 서버별 실제 설정은 Git 밖에 보관합니다. 새 서버의 문서는 새로 색인하며, 제목·한국어·식별자 검색 보강이 자동 적용됩니다.

## 배포 패키지 만들기

```powershell
python tools/package_docmind.py --output .local/packages/docmind.zip
python tools/package_docmind.py --verify .local/packages/docmind.zip
```

패키지는 커밋된 소스에서 DOCMIND 빌드·실행에 필요한 파일을 선별한 ZIP입니다. 테스트·개발 기록·범용 SDK·예제와 비공개 설정을 포함하지 않습니다. 파일별 SHA-256과 소스 커밋을 함께 기록합니다. 사전 빌드된 이미지 묶음은 아니며 대상 서버에서 의존성과 이미지를 준비해야 합니다. 구성 기준은 [패키지 안내](deploy/README.md)에 있습니다.

## 구성

| 경로 | 용도 |
|---|---|
| `app/` | DOCMIND API·검색·파싱·프런트와 이미지 빌드 소스 |
| `tools/windows/` | Windows 원본 감시·임시 복호화·색인 실행부 |
| `tools/local_embedding/` | 로컬 GTE 임베딩 서비스 |
| `deploy/` | 배포 패키지 구성과 안내 |
| `docs/` | DOCMIND 설치·운영·검증 기록 |

DOCMIND는 RAGFlow에서 파생된 검색·파싱·공통 API 코드를 사용합니다. 내부 모듈명, 데이터베이스 키, 컨테이너 내부 경로와 외부 의존 이미지명은 기존 실행 계약을 유지합니다. 원저작자 표기와 라이선스는 [LICENSE](app/LICENSE), [서드파티 고지](app/THIRD_PARTY_NOTICES.md)에 보존합니다.
