# 게임 기획 채용 대시보드

게임잡의 공개 채용 목록을 낮은 빈도로 읽어서, 게임 기획 관련 공고를 서울/경기 기준으로 필터링하고 HTML 대시보드로 정리합니다.

## 실행

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" .\job_dashboard.py
```

이미 저장된 데이터로 HTML만 다시 만들 때:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" .\job_dashboard.py --no-fetch
```

## 생성 파일

- `data/jobs.json`: 이번 실행에서 잡힌 최신 공고 목록
- `data/job_history.json`: 지금까지 발견한 모든 공고의 누적 이력
- `data/snapshots/YYYY-MM-DD_HHMMSS.json`: 실행 시점별 원본 수집 결과
- `output/dashboard.html`: 브라우저로 보는 대시보드

## 대시보드 보기

Windows 브라우저 주소창에는 아래 형식의 경로를 사용합니다.

```text
file:///C:/Users/ljkli/OneDrive/%EB%B0%94%ED%83%95%20%ED%99%94%EB%A9%B4/%EC%BD%94%EB%8D%B1%EC%8A%A4%20%EC%9E%91%EC%97%85/1.%20%EC%B7%A8%EC%97%85/output/dashboard.html
```

## 보존 방식

자동 갱신이 되어도 이전 공고는 `data/job_history.json`에 남습니다. 대시보드에서는 다음 기준으로 볼 수 있습니다.

- `전체 이력`: 지금까지 발견한 모든 공고
- `신규만`: 이번 실행에서 처음 발견된 공고
- `현재 활성`: 이번 실행에서도 다시 확인된 공고
- `이전 수집`: 예전에는 있었지만 이번 실행에서는 확인되지 않은 공고

공고별 상태값은 브라우저에 저장됩니다. 같은 브라우저에서 열면 `관심`, `지원 예정`, `지원 완료`, `보류` 선택이 유지됩니다.

## 컴퓨터가 꺼져 있어도 실행하기

컴퓨터가 꺼져 있어도 자동 수집하려면 이 폴더를 GitHub 저장소로 올리고 GitHub Actions와 GitHub Pages를 사용합니다.

준비된 파일:

- `.github/workflows/job-dashboard.yml`
- `output/.nojekyll`

동작 방식:

- GitHub Actions가 매일 한국 시간 오전 9시와 오후 6시에 `job_dashboard.py`를 실행합니다.
- 최신 결과는 `data/jobs.json`에 저장됩니다.
- 누적 이력은 `data/job_history.json`에 저장됩니다.
- 실행별 스냅샷은 `data/snapshots/`에 쌓입니다.
- `output/dashboard.html`은 GitHub Pages로 게시됩니다.

GitHub에 올린 뒤 Pages 배포가 끝나면 아래 같은 주소로 확인합니다.

```text
https://사용자이름.github.io/저장소이름/dashboard.html
```

## 설정

`config.json`에서 키워드, 우선 기업, 지역, 제외 키워드, 최소 점수, 저장 위치를 조정할 수 있습니다.

현재 기준:

- 직무: 게임 기획, 시스템/콘텐츠/밸런스/레벨/전투/스토리/라이브 기획
- 지역: 서울, 경기, 판교, 성남, 분당 등
- 우선 기업: 넥슨, 넥슨게임즈, 네오플, 넷마블, 넷마블네오, 넷마블에프앤씨
- 자동 실행: 매일 오전 9시, 오후 6시
