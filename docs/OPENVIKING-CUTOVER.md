# OpenViking 제품 경로 제거 및 전환 런북

이 문서는 PRD 11절 6단계의 개발·운영 전환 절차다. 현재 checkout에서는 제품 코드와 배포 설정의 호출·서비스·환경변수·패키지 의존성을 제거했다. **운영 컨테이너 중지나 운영 배포는 수행하지 않았다.** checkout 변경만으로 실행 중인 이미지가 바뀌었다고 판단하지 않는다.

평가용 인스턴스, 봉인 실험 결과와 복구 릴리스는 제품 런타임과 분리해 보존한다. 이 자료를 제품 Compose, 제품 네트워크 또는 제품 자격증명에 다시 연결하지 않는다.

## 자동 제거 검사

저장소 루트에서 다음을 실행한다.

```powershell
python .\app\scripts\verify_removed_runtime.py
python -m pytest .\app\test\unit_test\scripts\test_verify_removed_runtime.py -q
```

검사는 Git에 기록된 파일과 ignore되지 않은 새 파일을 함께 읽는다. 제품 코드, 테스트, Docker/Compose, 셸·PowerShell 도구, 환경 예제와 dependency lockfile에서는 이전 통합 이름과 프로토콜·환경 설정을 허용하지 않는다. 큰 tokenizer 자원과 바이너리 확장자는 실행 설정이 아니므로 제외한다.

예외는 스크립트의 `ALLOWED_EVIDENCE_FILES`에 파일 단위로 고정한다. 현재 허용 대상은 복구 manifest/checkpoint/검증 파일, 복구 안내·도구와 읽기 전용 trim 조사 자료뿐이다. 디렉터리나 glob 예외는 추가하지 않는다. 새 예외가 필요하면 다음을 모두 기록한다.

1. 제품 배포 입력이 아닌 이유
2. 자료 소유자와 보존 기간
3. 제품 이미지·Compose·네트워크·자격증명과 분리된 위치

## 개발 환경 전환

1. 작업 트리가 예상한 변경만 포함하는지 확인하고 자동 제거 검사를 통과시킨다.
2. [Windows 개발 Compose 안내](WINDOWS-DEVELOPMENT-DOCKER.md)에 따라 ignored 로컬 환경 파일을 생성한다. 실제 키를 stdout, diff 또는 검사 결과에 출력하지 않는다.
3. Compose 해석 결과를 검사한다.

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\windows\Test-WindowsDevelopmentCompose.ps1
   docker compose --env-file .\.local\docker\windows-dev.env `
     -f .\app\docker\docker-compose-windows-dev.yml --profile full config --format json `
     > .\.local\docker\windows-dev.resolved.json
   ```

4. 별도 프로젝트와 빈 개발 볼륨에서 인프라를 올린다. 운영 DB, 검색 색인, 객체 저장소와 원본 source를 mount하지 않는다.
5. 새 애플리케이션 이미지를 빌드하고 이미지 ID·digest·Git revision을 기록한다. 캐시된 이전 이미지를 실행 결과로 오인하지 않는다.
6. 합성 문서로 로그인, 등록, 파싱, BM25/dense C128 검색, 전체/폴더/문서 scope, Jina v3.5 Top5와 답변 인용을 검증한다.
7. 컨테이너 내부의 실제 배포 소스에도 이전 통합 이름이 없는지 읽기 전용으로 검사한다. 이미지 레이아웃에 맞는 루트에서 텍스트 소스·설정·설치 패키지를 검사하고 결과에는 파일 본문이나 비밀값을 남기지 않는다.
8. 재기동 후 동일 합성 문서와 활성 버전이 유지되는지 확인한다. 실패하면 개발 프로젝트만 내리고 볼륨을 보존해 원인을 조사한다.

개발 완료 증거에는 명령, 시간, commit, 이미지 digest, Compose 프로젝트명, 테스트 결과와 실패 항목을 남긴다. `docker compose down -v`, 이미지 prune, 볼륨 삭제는 이 검증 절차에 포함하지 않는다.

## 운영 전환 사전 조건

아래 조건이 모두 충족되기 전에는 기존 제품용 서비스를 중지하지 않는다.

- 새 이미지에서 자동 제거 검사와 컨테이너 내부 검사가 통과했다.
- 신규 DB와 업그레이드 대상 DB 양쪽에서 API·worker 기동과 필요한 migration을 검증했다.
- 별도 DB/index의 합성 및 승인된 canary로 기능 회귀·성능·오류율을 확인했다.
- DB, Elasticsearch, 객체 저장소와 작업 이력의 복구 지점을 만들고 격리 환경에서 복원을 실제 검증했다.
- 전환 중 들어오는 신규 문서 작업의 drain, 보류 또는 재처리 정책과 담당자를 정했다.
- 평가·봉인·복구 자료의 위치, digest, 접근권한과 보존 책임자를 기록했다.
- 롤백할 직전 제품 이미지 digest와 설정 snapshot을 확보했다. 설정 snapshot에는 비밀값을 포함하지 않는다.
- 유지보수 시간, 승인자, 관찰 담당자와 중단 기준을 정했다.

## 운영 전환 절차

1. 배포 직전 자동 제거 검사와 대상 Compose의 `config`를 다시 실행한다. 해석된 서비스·환경 key·volume·network 목록을 값이 아닌 이름과 digest 중심으로 기록한다.
2. 새 문서 작업 유입을 제어하고 진행 중 작업을 drain한다. 강제 종료된 작업은 성공 처리하지 않고 재시도 가능한 상태로 남긴다.
3. 배포 플랫폼에서 확인한 **정확한 기존 제품용 서비스 하나만** 중지한다. 프로젝트 전체 삭제나 volume 삭제를 하지 않는다. 평가용 인스턴스와 복구 영속 자료는 중지·삭제 대상이 아니다.
4. digest로 고정한 새 이미지를 배포하고 API와 worker의 readiness를 기다린다. checkout 수정 시각이 아니라 실행 컨테이너의 image ID로 적용 여부를 확인한다.
5. 로그인 및 health check 후 승인된 canary 문서로 다음을 순서대로 확인한다.
   - 등록과 활성 버전 전환
   - 전체/폴더 하위/선택 문서 scope
   - BM25 128 + dense 128, RRF60, 문서당 8, 전체 128
   - prefix 2400, Jina v3.5, Top5와 인용
   - 재시도·재시작·삭제 제외와 사용자별 대화 경계
6. 큐 깊이, 검색 오류, Jina 오류·usage, API p50/p95, memory와 임시 평문 정리 상태를 기준선과 비교한다.
7. 중단 기준을 넘지 않으면 작업 유입을 단계적으로 재개한다. 배포 기록에 최종 컨테이너·이미지 digest와 검증 결과를 남긴다.

## 중단 및 롤백

다음 중 하나면 추가 유입을 멈추고 롤백을 시작한다: 인증/source 경계 위반, 활성 버전 혼합, 검색 scope 누출, 지속적인 API/worker crash, 임시 평문 정리 실패, 복구 불가능한 migration 징후.

1. 신규 작업 유입을 차단하고 실패 시점 이후의 작업 ID와 문서 버전을 보존한다.
2. 검증한 직전 이미지와 설정 snapshot으로 애플리케이션을 되돌린다. DB/index를 되돌려야 한다면 사전에 검증한 복원 절차와 fencing 기준을 적용한다.
3. 전환 중 생성된 문서 작업을 대조해 누락·중복·최신 버전 역전을 확인한다.
4. 평가용 인스턴스를 제품 fallback으로 연결하지 않는다. 이전 통합을 재활성화하는 것은 별도 사고 변경 승인 없이는 롤백 수단이 아니다.
5. 원인·영향 범위·복원 결과를 기록하고 자동 제거 검사를 다시 통과한 뒤에만 재배포한다.

## 현재 단계의 완료와 미완료

- 완료: 저장소 제품 경로의 정적 호출·의존성·환경변수·서비스 제거, 재발 방지 검사와 합성 단위 테스트, 개발·운영 cutover 절차 작성.
- 미완료: 실제 운영 이미지 내부 검사, 운영 DB/index 복원 훈련, 승인된 canary와 성능 측정, 제품용 서비스 중지와 운영 배포.

이 구분을 유지해 정적 검사 통과를 운영 전환 완료로 표시하지 않는다.
