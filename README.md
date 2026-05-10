# 메르AI 포트폴리오 자동 분석

## 전체 구성 요약

이 프로젝트는 **3가지 방식**으로 메르AI를 사용할 수 있습니다.

| 방식 | 위치 | 자동화 | 필요한 것 |
|------|------|--------|---------|
| **GitHub Actions 자동화** | 이 폴더 | ✅ 격주 자동 실행 | Gemini API 키 (무료) |
| **ChatGPT GPTs 페르소나** | `personas/chatgpt-gpt/` | 수동 트리거 | ChatGPT Plus (이미 있음) |
| **Gemini Gems 페르소나** | `personas/gemini-gem/` | 수동 트리거 | Gemini Advanced (이미 있음) |

→ 셋 중 하나만 써도 되고, 셋 다 써도 됩니다.
→ 채팅으로 빠르게 쓰고 싶으면 Gems, 자동으로 쌓아두고 싶으면 GitHub Actions.

메르(blog.naver.com/ranto28)의 블로그 글을 자동 수집해서
Gemini AI가 메르 스타일로 한국·미국 주식 포트폴리오를 분석해주는 시스템.

prophit_(blog.naver.com/prophit_)이 수동으로 하던 "메르ai포트"를 완전 자동화한 버전.

**비용: $0** (Gemini API 무료 티어 + GitHub Actions 무료)

---

## 작동 방식

```
[GitHub Actions 스케줄러 — 매월 1일, 15일 자정]
        ↓
[메르 블로그 RSS 파싱 + 전문 스크래핑]
        ↓
[Gemini 2.5 Pro에게 분석 요청]
        ↓
[output/report_YYYYMMDD.md 로 자동 저장 + 커밋]
```

---

## 셋업 (5분)

### 1단계 — Gemini API 키 발급 (무료)

1. https://aistudio.google.com/app/apikey 접속
2. Google 계정으로 로그인
3. **"Create API key"** 클릭
4. 발급된 키 복사 (형식: `AIza...`)

> 무료 한도: Gemini 2.5 Pro 기준 일 25회. 격주 실행이면 여유있게 충분.

---

### 2단계 — GitHub 저장소 만들기

1. https://github.com/new 에서 새 레포 생성
   - 이름 예: `mer-portfolio`
   - **Private** 권장 (투자 정보이므로)
   - README 체크 해제

2. 이 프로젝트 파일들을 레포에 업로드
   ```
   mer-portfolio/
   ├── .github/workflows/schedule.yml
   ├── output/.gitkeep
   ├── fetch_mer.py
   ├── system_prompt.py
   ├── analyze.py
   ├── main.py
   └── requirements.txt
   ```

---

### 3단계 — API 키를 GitHub Secrets에 등록

1. 레포 페이지 → **Settings** 탭
2. 왼쪽 메뉴 → **Secrets and variables** → **Actions**
3. **"New repository secret"** 클릭
4. Name: `GEMINI_API_KEY`
5. Secret: 1단계에서 복사한 키 붙여넣기
6. **"Add secret"** 클릭

---

### 4단계 — 첫 실행 테스트

1. 레포 → **Actions** 탭
2. 왼쪽에서 **"메르AI 포트폴리오 분석"** 클릭
3. **"Run workflow"** 버튼 클릭 → **"Run workflow"** 확인
4. 실행 완료 후 `output/latest.md` 파일 확인

---

## 자동 실행 스케줄

| 실행 시점 | 수집 기간 |
|-----------|-----------|
| 매월 1일 자정 (KST) | 직전 14일 |
| 매월 15일 자정 (KST) | 직전 14일 |

수동으로도 언제든 실행 가능 (Actions → Run workflow).

---

## 출력 파일

```
output/
├── latest.md              ← 항상 최신 리포트
├── report_20260515.md     ← 날짜별 히스토리
├── report_20260501.md
└── ...
```

리포트 형식:
```markdown
# 메르AI 포트폴리오 리포트
분석 기간: 2026-05-03 ~ 2026-05-15

## 📌 시장 분석 핵심 인사이트
### 인사이트 1: 호르무즈 봉쇄 장기화와 조선업 나비효과
1. 확인된 사실 ...
해석(나비효과): ...
투자판단: Buy 강 — ...

## 📊 포트폴리오 추천
### 🇰🇷 국내주식
| 종목명 | 코드 | 판단 | 목표비중 | 핵심 근거 |
...
### 🇺🇸 해외주식
...
```

---

## 환경변수 옵션

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `GEMINI_API_KEY` | (필수) | Google AI Studio API 키 |
| `FETCH_DAYS` | `14` | 수집할 최근 일수 |
| `GEMINI_MODEL` | `gemini-2.5-pro` | 사용할 모델 |
| `OUTPUT_DIR` | `output` | 리포트 저장 경로 |

로컬 실행 예:
```bash
# 기본 실행
export GEMINI_API_KEY="AIza..."
python main.py

# 최근 30일 수집, 빠른 모델 사용
FETCH_DAYS=30 GEMINI_MODEL=gemini-2.0-flash python main.py
```

---

## 모델 폴백 순서

API 키 할당량 초과나 모델 미지원 시 자동으로 다음 모델로 전환:

1. `gemini-2.5-pro` (최고 품질, 일 25회 무료)
2. `gemini-2.5-pro-preview-05-06`
3. `gemini-2.0-flash-thinking-exp-01-21`
4. `gemini-2.0-flash` (일 1500회 무료, 품질 약간 낮음)
5. `gemini-1.5-pro`

---

## 로컬 개발 환경 셋업

```bash
# 패키지 설치
pip install -r requirements.txt

# 스크래핑만 테스트
python fetch_mer.py

# 분석만 테스트 (API 키 필요)
export GEMINI_API_KEY="AIza..."
python analyze.py

# 전체 실행
python main.py
```

---

## 주의사항

- 이 리포트는 **참고용**이며 투자 권유가 아닙니다
- 메르 블로그 분석을 AI가 재해석한 것으로, 실제 메르의 견해와 다를 수 있습니다
- 네이버 블로그 구조 변경 시 스크래핑이 일시 중단될 수 있습니다
  - 이 경우 RSS 요약본으로 자동 폴백됩니다
- Gemini 무료 모델이 업데이트되면 `GEMINI_MODEL` 환경변수로 조정하세요
