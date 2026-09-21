# 구성 C 이식 계약

운영 문서나 기존 평가 청크 없이 구현할 수 있도록 알고리즘 계약을 기록한다. 기준은 PRD 10절이며, 기존 코드는 OpenViking을 사용하는 변경 전 상태다.

1. 같은 source/tenant/선택 scope/권한/활성 버전 조건을 BM25와 dense 양쪽 후보 생성 전에 적용한다.
2. BM25 최대 128개, dense 최대 128개를 독립 실행한다. dense에 키워드 일치 조건을 붙이거나 부족분 보충 검색으로 사용하지 않는다. dense k=2048, num_candidates=4096, similarity=0.0.
3. 각 lane의 rank는 1부터 시작한다. 같은 chunk ID에 대해 각 lane의 1/(60+rank)를 합산한다. 가중치는 동일하다. RRF 점수 내림차순, 동점 chunk ID 오름차순.
4. 이 순서를 순회하며 문서당 최대 8청크, 전체 최대 128청크로 제한한다. scope별 quota나 AI routing은 없다.
5. 후보를 chunk ID 순서로 정렬해 API 요청을 안정화한다. 청크별 앞 2,400문자만 Jina hosted API의 jina-reranker-v3.5에 전달한다. tokenizer·저장 청크·embedding을 이 상한에 맞춰 바꾸지 않는다.
6. Jina 응답 index를 요청 순서의 chunk ID에 정확히 되돌린다. Jina 점수→RRF 점수 내림차순→chunk ID 오름차순으로 정렬해 최대 Top5를 반환한다. 빈 후보는 API 호출 없이 빈 결과.
7. atomic_decomposition=false. API 오류 시 v3로 몰래 변경하거나 OpenViking으로 우회하지 않는다. 실패 정책은 코드에서 명시하고 실제 UI에 구분한다.

필수 합성 테스트: 각 lane만의 후보, 공통 후보, 동점, 문서당 제한, 전체 제한, 폴더 하위/선택 문서, 빈 scope, 권한/비활성 제외, 2400자 경계(한글 포함), 요청 정렬과 응답 index 매핑, 외부 API 오류. 한국어 문자 수 계약은 Python의 문자열 단위를 기준으로 구현하고 프런트 UTF-16 length로 임의 대체하지 않는다.

기존 r4/E3 수치는 PRD에만 기록돼 있으며 v3 결과다. 데이터와 API 키는 전달하지 않는다. v3.5 실측은 새 시험 데이터와 새 credential로 수행한다.
