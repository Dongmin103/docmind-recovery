# Windows 개발 인수인계

- 개발 브랜치: [windows-development-handoff](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff)
- [START_HERE.md](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff/START_HERE.md)
- [PRD.md](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff/PRD.md)
- [Windows 개발 안내 및 서버 Codex 전달 문구](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff/WINDOWS_DEVELOPMENT.md)
- [현재 구조](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff/docs/CURRENT-ARCHITECTURE.md)
- [검증 기록](https://github.com/Dongmin103/docmind-recovery/tree/windows-development-handoff/HANDOFF-VERIFICATION.json)

소스 6,047개를 포함하며 GitHub에서 별도로 clone해 모든 소스 SHA-256과 git fsck를 확인했습니다. 기존 원본 PDF·운영 청크/벡터·DB·객체·실제 환경설정·개인키는 포함하지 않습니다. 일반 합성 테스트 fixture와 UI/파서 자원은 포함합니다.

```powershell
git clone --branch windows-development-handoff --single-branch https://github.com/Dongmin103/docmind-recovery.git C:\DocMindDev\docmind
Set-Location C:\DocMindDev\docmind
Get-Content .\START_HERE.md
```

서버 쪽 Codex는 루트 AGENTS.md부터 읽으면 됩니다. 소스는 app/이며 새 PRD 기능 구현 전의 기준 코드입니다. 실제 실행에는 Windows Docker 환경, 서버에 설치된 uEncryptor2, 새 설정/API 키 및 모델 다운로드가 필요합니다. 기존 클라우드 서비스나 원본을 직접 변경하지 말고 분리된 개발 환경에서 시작합니다.

발행 시 개발 브랜치 커밋: `5485f2df13fc16b2cac279d324a7bb0749ebd851`. 기존 복구 릴리스와 태그는 보존했습니다.
