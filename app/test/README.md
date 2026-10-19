# DOCMIND 회귀 검증

테스트는 소스 저장소에서 개발·검증할 때 사용하며 배포 ZIP에는 포함하지 않습니다.
프로젝트 의존성을 준비한 격리 환경에서 변경 범위의 테스트만 실행합니다.
실제 문서·모델 키·운영 설정을 단위 테스트에 전달하지 않습니다.

```bash
# app/ 기준, 합성 설정과 프로젝트 Python 환경에서 실행
python -m pytest test/unit_test/rag/nlp/test_docmind_lexical.py \
  test/unit_test/rag/parser_platform/test_search_fields.py \
  test/unit_test/rag/utils/test_docmind_lexical_scope.py
```

프런트는 `app/web`에서 `npm test -- --runInBand --coverage=false src/pages/docmind`
및 `npm run build`로 확인합니다. 저장소 루트의 패키지 경계·변조 검증은
`python -m unittest discover -s tools/tests -p test_package_docmind.py -v`입니다.

범용 RAGFlow SDK와 해당 SDK 통합 테스트는 이 DOCMIND 저장소에서
제거했습니다. 인증·문서 처리 등 현재 공통 코드의 회귀 테스트는 유지합니다.
