# 남은 수집 검증의 해결 경로 — 2026-09-28

이 문서는 실제 확인한 제약과 다음 적용 방법을 구분한다. 원문, 키, 설정 파일 내용은 포함하지 않는다. 네 DOC의 처리 결과는 [실제 검증 보고서](REMAINING-INGESTION-VALIDATION-2026-09-28.md)를 참고한다.

후속 적용 결과는 [제한된 자동 동기화와 Surya 복구 실환경 검증](LIVE-SYNC-RECOVERY-VALIDATION-2026-09-28.md)을 따른다.
아래의 “미적용” 설명은 조사 시점 기록이다. 이후 source별 상주 작업, Surya readiness,
claim 준비 검사, 명시적 재처리 및 재처리 중 검색 유지 수정이 개발 서버에 적용됐다.
사용자의 상시 켜짐 조건에 따라 로그아웃/S4U 검증은 후순위로 남겼다.

## 1. 로그인에 의존하는 Windows 실행

현재 KeepAlive, Host Worker, Source Watcher, 자정 정합성 작업은 같은 `uplex` 계정의 Interactive 작업이다. Ubuntu WSL도 이 사용자에게 등록되어 있다. 다른 서비스 계정이나 SYSTEM으로 principal만 바꾸는 방식은 WSL과 파일 접근 권한을 그대로 보존한다고 볼 수 없다.

우선 시험할 방법은 **같은 사용자의 S4U 작업**이다. 암호를 대화나 저장소로 전달하지 않고 비대화형 실행 가능성을 확인할 수 있다. 다만 S4U는 네트워크 및 EFS 파일 접근에 제약이 있으므로, 실제 계정에서 WSL 실행, Docker 응답, 로컬 source 읽기, 복호화 프로그램 실행을 각각 검증해야 한다. 제품 자체의 암호화 파일과 Windows EFS는 서로 다른 개념이다.

이번 임시 S4U probe는 `Register-ScheduledTask` 단계에서 Windows의 **액세스 거부**로 실패했다. 현재 실행 프로세스는 승격되지 않았다. 시험 작업 및 결과 파일은 생성되지 않았으며 S4U 실행 자체의 성공 여부는 아직 모른다. 기존 작업은 변경하지 않았다. 이 오류는 이전 자동 명령 검토의 정책 거절과 별개다.

실행 순서:

1. 관리자가 같은 Windows 사용자로 제한된 S4U probe를 등록한다.
2. WSL/Docker와 로컬 시험 파일 접근을 확인한다. 실패하면 실제 오류 코드를 보고 계정의 batch logon 권한과 S4U 제약을 구분한다.
3. 통과한 뒤 KeepAlive와 worker/watch 작업을 함께 전환한다. 일부만 전환하면 로그인 의존성이 남는다.
4. 진행 중 작업과 임시 평문이 없는 상태에서 로그아웃 및 재부팅 후 API 응답, 승인 DOC 처리, 정리까지 확인한다.
5. S4U가 필요한 접근을 제공하지 못하면 동일 사용자 Password 방식 또는 별도 서비스 계정으로 WSL/ACL까지 이전하는 방식을 검토한다. Password 방식의 암호 입력은 Windows의 로컬 등록 절차에서 처리한다.

공식 근거: [Task Scheduler LogonType](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype), [WSL의 사용자별 설치](https://learn.microsoft.com/en-us/windows/wsl/setup/environment).

관리자 실행용 [S4U 진단 절차](S4U-PROBE-2026-09-28.md)와 [진단 스크립트](../tools/windows/Test-DocMindS4UContext.ps1)를 준비했다. 이 스크립트는 복호화를 실행하지 않는다. 파일 존재·루트 접근·WSL/Docker 응답 확인 이후 실제 복호화와 로그아웃 검증이 별도로 필요하다.

## 2. 승인 source만 자동 감시·정합성 실행

TEST1 및 DEPT_1의 DOC 네 개는 사용자 승인 범위다. DEPT_2 전체 382개 수집은 별도 범위다. 기존 등록기의 `-AllowFullScan`은 모든 설정 source를 대상으로 하므로 그대로 켜는 방식으로 해결하지 않는다.

감시는 이미 `-SourceId`를 지원한다. 자정 정합성도 서명된 claim에 같은 source 선택을 전달하고, 서버가 해당 활성 source만 선택하며 Windows가 응답 source를 다시 확인해야 한다. 재시도 작업 선택도 같은 범위를 적용해야 한다. 비활성 source의 오래된 schedule이 선택되지 않아야 한다.

이 조사에서 위 claim/재시도 범위 제한 코드를 수정했다. 추가로 이미 `RETRY_WAIT`인 작업을 매 정합성 호출마다 다시 예약해 실행 시각을 뒤로 미룰 수 있던 동작을 제거했다. 이 상태의 작업은 지정 시각이 되면 기존 ingestion worker가 직접 claim하므로 재예약 대상에 포함하지 않는다. 실제 앱 재시작 및 상주 작업 전환은 이번 조사에서 하지 않았다.

상주 구성은 TEST1 및 DEPT_1 각각의 source를 지정한 watcher/정합성 작업으로 구성하고, DEPT_2는 승인된 기존 문서 관찰 범위를 유지한다. 등록기에는 source별 작업 이름과 인자 처리가 추가로 필요하다. 코드 수정과 실제 작업 등록·source 활성화는 서로 다른 완료 단계다.

구체적으로 TEST1은 `-SourceId home-test1-e2e`, DEPT_1은 `-SourceId dept-1-e2e`를 사용한다. source별 감시 작업은 `Watch-DocMindEncryptedSources.ps1 -EnableDiscovery`, 정합성 작업은 `Invoke-DocMindSourceReconciliation.ps1 -Reason scheduled`에 해당 인자를 추가한다. 기존 범위 미지정 자정 작업은 비활성을 유지한다. 현재 static 문서 매핑은 DEPT_2 한 건이고 기존 watcher에는 `-EnableDiscovery`가 없으므로 그 관찰 동작을 유지할 수 있다.

완료 기준은 선택 source 외 scan/job 변화가 없고, 중복 이벤트와 실패 재시도 이후에도 범위가 유지되며, 실제 Asia/Seoul 자정 실행 기록이 남는 것이다.

추가 경계: 현재 ingestion worker의 기존 대기 작업 claim은 `source.enabled`를 직접 검사하지 않는다. source 비활성화가 이미 생성된 대기 작업까지 중지시킨다고 보장할 수 없다. 이번 수정은 정합성 claim 및 실패 재예약의 범위를 제한하며, 이미 대기 중인 작업의 source 비활성화 처리까지 완료한 것은 아니다. 상주 전환 전에 진행 중/대기 작업을 확인하고 이 경계도 별도 검증한다.

Host Worker 자체도 source별 큐가 아닌 전역 큐를 claim한다. 따라서 이번 제한은 지정한 발견·정합성 경로의 범위 보장이지, 다른 관리 API에서 만들어진 DEPT_2 작업까지 차단하는 worker allowlist는 아니다.

## 3. Surya 660초 종료

확인된 사실:

- 첫 TEST1 DOC에서 Office media watchdog 660초가 만료되어 exit 124로 재시작했다. OOM 종료는 아니었다. 이후 문서에서는 GPU OCR 응답이 성공했다.
- 현재 서비스는 `SuryaInferenceManager(..., lazy=True)`를 사용한다. `/health` 200은 첫 모델 기동 및 OCR 완료를 증명하지 않는다.
- 설치된 Surya 0.22.1의 모델 기동 제한은 900초다. 첫 요청에서는 이미지 준비, lazy 모델 기동, 추론이 모두 바깥 660초 watchdog 안에 들어간다.
- 내부 추론 호출 제한은 600초이며 Surya batch 함수는 오류 또는 반복 출력에 최대 세 번 추가 시도한다. 따라서 600초 설정이 전체 처리 시간의 상한은 아니다.

이 시간 제한 불일치는 확인했지만, 기존 실패가 모델 기동 지연인지 추론 정지인지 현재 증거로 확정할 수 없다.

수정 방향은 다음과 같다.

1. 모델 시작, 이미지 준비, 추론, 결과 검증 단계의 시간과 성공/실패 코드만 기록한다. 이미지·본문·프롬프트를 로그에 남기지 않는다.
2. liveness와 모델 readiness를 분리한다. 첫 실제 DOC 요청 전에 제한된 모델 기동과 합성 OCR로 준비를 확인한다. 모델 기동 성공과 OCR 성공도 구분한다. 기존 기동 예산 900초를 별도 준비 단계에 적용하는 것이 첫 후보이며, OCR의 600/660/720초 제한은 원인 확인 없이 늘리지 않는다.
3. 기동 예산을 문서 추론 watchdog에서 분리하고, 추론의 개별 호출 및 재시도 합계가 전체 deadline을 넘지 않도록 한다. 현재 pinned Surya에는 SDK 재시도와 batch 재시도를 함께 제어하는 공개 설정이 확인되지 않았다. 이 부분은 명시적인 dependency 패치 또는 adapter 변경과 회귀가 필요하며 환경변수 하나로 해결됐다고 표시하지 않는다.
4. 재시작 복구 후 승인된 실패 DOC를 다시 처리한다. `PARSER_SURYA_UNAVAILABLE` 제거, GPU 실행 증거, Windows/Docker 정리, 활성 버전 교체 및 검색을 확인한다.
5. cold/warm 시작을 구분해 반복 검증하고 각 단계 시간을 기록한다. 재현되지 않은 원인을 해결했다고 단정하지 않는다.

Docker health만 변경하는 것으로는 충분하지 않다. 현재 검색 앱은 Surya와 독립적으로 기동한다. 수집 측에서도 모델 준비를 제한된 시간 동안 기다리거나 재예약해야, Surya 재시작 직후 문서를 바로 경고 COMPLETE로 확정하는 상황을 줄일 수 있다. 검색 앱 전체를 OCR 준비에 묶지 말고 OCR이 필요한 수집 경로에서 처리한다. 준비 대기와 실제 OCR 실패는 서로 다른 상태로 검증한다.

이 조사에서는 GPU 컨테이너를 재시작하거나 새 모델을 설치하지 않았다. Surya 변경은 아직 적용하지 않았다.

## 4. 같은 경로의 본문 A→B 교체 검증

서로 다른 본문의 승인 암호문은 확보되어 있으므로 추가 문서 승인이 필요한 상태는 아니다. 남은 것은 원본 밖의 격리된 시험 source와 동일 논리 경로다.

기존 개발 fixture source를 재사용하여 암호문 복사본 A를 색인하고 같은 시험 경로를 B로 교체하는 방식이 적절하다. 복호화된 본문 해시가 다른 표본을 사용한다. B 처리 중에는 A가 활성 상태를 유지하고, B 활성화 후에는 B만 검색되며 A가 retained가 되는지 확인한다. 원본 네 파일의 해시와 임시파일 정리 결과도 확인한다.

다만 이전의 live 설정 읽기·fixture 설정 생성·scan을 합친 명령은 자동 검토에서 `blocked by policy`로 거절되었다. 세부 사유는 제공되지 않았다. 이번 조사에서는 그 명령을 다른 도구나 표현으로 재실행하지 않았다. 따라서 fixture 환경 복원 및 A→B 실제 시험은 여전히 미완료다. 기존 합성 회귀 통과를 실제 시험 통과로 대체하지 않는다.

## 적용 순서

1. source 제한 claim/재시도 코드와 회귀를 마무리한다.
2. 관리자 S4U probe와 Surya readiness/deadline 수정을 독립적으로 진행한다.
3. 승인 source만 상주 등록하고 Surya 경고 문서를 재처리한다.
4. 격리 A→B 및 실제 자정·로그아웃·재부팅 검증을 수행한다.

운영 source 전체 감시, 원본 쓰기, 로그아웃 이후 동작은 위 검증 완료 전까지 완료로 표시하지 않는다.

## 이번 변경 및 검증 결과

- 코드 및 S4U 진단 도구 커밋: `b198727`.
- 일회용 앱 컨테이너의 정합성 SQLite 회귀: 24/24 통과. 선택 source, 비활성 source 제외, 다른 source 실패 작업 불변, 기존 retry deadline 보존을 포함한다.
- Windows Host Worker 자체 검사 통과. 수정한 PowerShell 두 파일 및 문서 명령 블록의 구문 오류 0.
- 현재 Interactive 세션에서 S4U 진단 스크립트의 boolean 7개 모두 true. 결과 파일은 제거했다. 이것은 S4U 실행 검증이 아니다.
- Ruff check는 Windows read-only bind mount의 실행 비트에 따른 EXE002만 제외하고 통과했다. Ruff format check는 기존 형식 차이로 실패했으며 관련 없는 전체 파일 재포맷은 하지 않았다.
- 앱 재시작·이미지 재생성·source 활성화·기존 예약 작업 변경은 하지 않았다. 신규 코드의 실제 실행 반영 및 브라우저 검증은 다음 적용 단계다.
