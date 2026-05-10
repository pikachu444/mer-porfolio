# ChatGPT GPTs — 메르AI 페르소나 설정 가이드

## 필요한 것

- ChatGPT Plus 구독 ($20/월, 이미 있음)
- 설정 시간: 약 5분

---

## 설정 순서

### 1단계 — GPTs 생성 페이지 열기

1. https://chatgpt.com 접속
2. 왼쪽 사이드바 하단 → 본인 프로필 클릭
3. **"My GPTs"** → **"Create a GPT"** 클릭

또는 바로 접속: https://chatgpt.com/gpts/editor

---

### 2단계 — Configure 탭 선택

- 상단 탭에서 **"Configure"** 클릭 (Create 탭 말고)

---

### 3단계 — 기본 정보 입력

| 항목 | 입력값 |
|------|--------|
| **Name** | `메르AI` |
| **Description** | `메르(blog.naver.com/ranto28) 블로그 분석 기반 한국·미국 주식 포트폴리오 추천` |
| **Profile picture** | 원하는 이미지 업로드 (선택사항) |

---

### 4단계 — Instructions 입력 (핵심)

`instructions.md` 파일의 **코드블록 안 내용 전체**를 복사해서
**Instructions** 텍스트 박스에 붙여넣기.

---

### 5단계 — Capabilities 설정

아래 항목 체크 여부:

| 기능 | 설정 | 이유 |
|------|------|------|
| **Web Search** | ✅ 켜기 | 메르 블로그 RSS 자동 조회 가능 |
| **Canvas** | ✅ 켜기 | 표 형식 리포트 렌더링 |
| **Code interpreter** | ❌ 꺼도 됨 | 불필요 |
| **Image generation** | ❌ 꺼도 됨 | 불필요 |

> Web Search를 켜면 사용자가 메르 글을 직접 붙여넣지 않아도 GPT가 RSS를 읽어 분석합니다.

---

### 6단계 — Conversation starters 설정 (선택)

아래 내용을 Conversation starters에 추가하면 채팅창에 버튼으로 뜸:

```
최근 2주 메르 글 분석해서 포트폴리오 추천해줘
오늘 메르 글 읽고 한국 주식 추천해줘
아래 메르 글 붙여넣을게, 분석해줘
이번 주 섹터 온도계 업데이트해줘
```

---

### 7단계 — 저장 및 공개 범위 설정

1. 우측 상단 **"Save"** 클릭
2. 공개 범위 선택:
   - **Only me** — 나만 사용 (권장, 투자 정보)
   - **Anyone with a link** — 링크 있는 사람만

---

## 사용 방법

### 방법 A — 자동 (Web Search 켰을 때)
```
최근 2주 메르 글 분석해서 포트폴리오 추천해줘
```
→ GPT가 직접 RSS 피드(rss.blog.naver.com/ranto28.xml)를 읽고 분석

### 방법 B — 수동 (글 붙여넣기)
```
아래 메르 글 분석해줘:

[메르 블로그에서 복사한 글 붙여넣기]
```

### 방법 C — 자동화 파이프라인 결과물 입력
GitHub Actions로 생성된 `output/latest.md` 내용을 붙여넣고
```
위 자동 수집 결과를 바탕으로 포트폴리오 리포트 다시 정리해줘
```

---

## 팁

- Web Search가 켜져 있어도 네이버 블로그는 로그인 없이 접근 가능한 부분만 읽힘
- 전문이 안 읽히면 "RSS 피드에서 제목과 요약만 읽어서 분석해줘"라고 하면 됨
- 분석 주기마다 새 대화를 시작하면 컨텍스트가 깔끔하게 유지됨
- GPT 메모리 기능을 켜두면 이전 포트폴리오 대비 변경사항을 추적해줌
