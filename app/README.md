# DOCMIND 애플리케이션

이 디렉터리는 DOCMIND 실행 이미지의 빌드 소스입니다. 설치·원본 폴더 등록·실행은 [저장소 README](../README.md)와 [서버 배포 안내](../docs/SERVER-DEPLOYMENT-2026-10-19.md)를 따릅니다.

```powershell
# 저장소 루트에서 실행
docker build -f app/Dockerfile -t docmind:2026.10.19 ./app
```

- `api/`, `common/`: 로그인·프로젝트·문서·원본 동기화·검색·답변 API
- `rag/`, `deepdoc/`: DOCMIND가 사용하는 검색·청킹·파싱·OCR 공통 코드
- `parser_services/`: 파서와 원문 미리보기 서비스
- `web/`: DOCMIND 화면
- `docker/`, `conf/`: 이미지 실행과 설정 예제
- `ragflow_deps/`: 빌드에 필요한 외부 자원 준비 코드

검색은 동일 범위의 BM25 128 + dense 128 → RRF(k=60) → 문서당 최대 8개·전체 최대 128개 → 본문 앞 2,400자·Jina v3.5 → 최종 5개 계약을 사용합니다. 임베딩은 GTE multilingual base 768차원입니다.

RAGFlow에서 파생된 코드는 DOCMIND 실행 구성요소로 포함하며 원저작자 표기를 유지합니다. [LICENSE](LICENSE)와 [서드파티 고지](THIRD_PARTY_NOTICES.md)를 확인하세요. 범용 RAGFlow SDK·다국어 소개·문서 사이트·예제·Helm 배포물은 포함하지 않습니다.
