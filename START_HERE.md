# 여기서 시작하세요

DOCMIND 배포 소스는 `main` 브랜치입니다. 다른 서버에 배포할 때는 [README](README.md)와 [서버 배포 안내](docs/SERVER-DEPLOYMENT-2026-10-19.md)를 먼저 읽으세요. 소스 실행 패키지 생성 방법은 [패키지 안내](deploy/README.md)에 있습니다. 아래 문서는 개발 요구사항과 검증 기록입니다.

1. [PRD.md](PRD.md): 최종 요구사항과 사용자 결정
2. [WINDOWS_DEVELOPMENT.md](WINDOWS_DEVELOPMENT.md): 받기·설정·서버 Codex 인수인계 문구
3. [현재 구조](docs/CURRENT-ARCHITECTURE.md): 코드와 서비스 연결
4. [인수인계 상태](docs/HANDOFF-STATUS.md): 포함/제외 및 검증 한계
5. [RAGFlow 축소 조사](docs/RAGFLOW-TRIM-AUDIT.md): 제거 후보와 공통 의존
6. [C 검색 계약](docs/C-SEARCH-CONTRACT.md): C128·prefix2400·Jina v3.5 이식 기준

실제 소스는 **app/** 아래에 있습니다. 원본 PDF, 운영 청크·벡터·DB·MinIO 파일은 필요하지 않으며 포함하지 않았습니다. 기존 암호화 복구 릴리스를 받아 풀지 마세요. 이 브랜치만 clone하면 코드 작업을 시작할 수 있습니다. 실제 실행에는 서버의 Docker/복호화 도구와 새 환경설정·API 키·모델 다운로드가 필요합니다.
