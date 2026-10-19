# DOCMIND 배포 패키지

`python tools/package_docmind.py --output .local/packages/docmind.zip`으로
현재 커밋의 DOCMIND 소스 배포 패키지를 만든다. ZIP을 풀고 루트 README의
서버 설정·이미지 빌드·원본 등록·색인 실행 절차를 따른다.

GitHub Actions의 `DOCMIND package` 실행도 같은 ZIP을 생성·검증한 뒤
`docmind-<커밋 SHA>` artifact로 제공한다. 성공한 실행의 Artifacts에서 받는다.

`package.json`에 포함할 경로를 명시한다. 애플리케이션, 프런트 빌드 소스,
파서, Docker 구성, Windows 워커와 GTE 도구를 포함한다. 설치 문서의 로컬
링크는 필요한 문서만 따라 포함한다. 범용 SDK·예제·Helm·테스트·개발용
벤치마크·복구 기록·실제 설정과 키·원본·색인은 배포 ZIP에 넣지 않는다.

빌더는 Git 커밋의 blob을 읽으므로 미추적·ignored 파일이나 작업 중인 변경은
들어가지 않는다. 변경을 먼저 커밋해야 한다. 심볼릭 링크와 예상하지 않은
배포 파일 형식은 거부한다. 내부 `docmind-manifest.json`에 소스 커밋과
파일별 SHA-256을 기록하며 `--verify <ZIP>`으로 누락·추가·변조를 확인한다.

이 ZIP은 **대상 서버에서 빌드하는 소스 실행 패키지**다. Docker 이미지,
모델 가중치와 상용 복호화 실행 파일을 포함한 오프라인 설치본은 아니다.
서버별 원본 경로·모델 연결·인증 키·런타임 Compose는 대상 서버에서 준비한다.
지원하지 않는 일반 평문 수집이나 로그아웃 무인 실행을 자동 제공하지 않는다.

RAGFlow 파생 모듈은 DOCMIND가 실제 사용하는 공통 실행 코드다. 내부 경로와
데이터 계약을 이름만 바꾸어 끊지 않으며 원저작자·라이선스 표기를 보존한다.
