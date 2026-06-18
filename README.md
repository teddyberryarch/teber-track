# teber-track — 1년 묻지마 투자 신호등

메모리 슈퍼사이클 1년 베팅을 **안 보고 버티기** 위한 시스템.
정확한 금액은 일부러 숨긴다 — 위치(이정표 단계)와 거리(체크선)만 본다. "애매해야 길게 간다."

## 작동
야후 시세로 5종목(하닉레버·SNXX·SNDU·MUU·SOXL) 전체 평가액을 원화로 합산 →
- **이정표**: 시작 평가액에서 +30% 복리(1.3^n). 한 칸 넘으면 🚀 알림 + 체크선 상향.
- **체크선**: max(6,000만 고정, 사상고점 × 0.5). 닿으면 🔴 긴급 점검 알림.
- **신호등**: 🟢 안전 / 🟡 근접 / 🔴 점검.

GitHub Actions가 하루 2회 실행 → 서버 없음, 비용 0. 자동주문 없음(알림만).

## 구성
- `main.py` — 시세 → 이정표/체크선 판정 → 이벤트 시 텔레그램 → data.json/state.json 저장
- `holdings.json` — 보유 주수 + 설정(시작값·이정표·체크선). 거래하면 여기 수정
- `index.html` — 신호등 대시보드 (금액 숨김)
- `thesis.html` — 왜 이 베팅을 하는가 (다짐·룰북). 빠질 때 읽는 용
- `.github/workflows/check.yml` — 하루 2회 cron
- `data.json` / `state.json` — 봇이 자동 생성·커밋

## 셋업
1) **Secrets** (repo → Settings → Secrets and variables → Actions)
   - `TG_TOKEN` : 텔레그램 봇 토큰 (BotFather)
   - `TG_CHAT_ID` : 7958777842
2) **Actions 권한** (Settings → Actions → General → Workflow permissions → "Read and write")
3) **Pages** (Settings → Pages → Source: main, /(root))
   → `https://teddyberryarch.github.io/<repo>/`
4) **첫 실행**: Actions 탭 → "teber-track" → Run workflow

## 내일(6/19) 매수 후 할 일
1. `holdings.json` 의 `shares` 갱신:
   - sndu: 0 → 84 (실제 체결 주수)
   - snxx: 633 → 매수 후 합계
   - muu, soxl 도 합계로
2. 매수 끝나면 `_config.start_value_krw` 에 **시작 평가액(원화)** 입력.
   - 0으로 두면 봇이 첫 실행 때 현재 평가액을 시작점으로 자동 고정.
   - 권장: 2차 매수 직후 나무 총자산(원화)을 넣어 깔끔하게 시작.

## 거래/리밸런싱하면
`holdings.json` 의 해당 종목 `shares` 만 고치면 됨. 시세는 봇이 야후에서 자동.

## 이정표 / 체크선 조정
`holdings.json` → `_config`:
- `milestone_growth`: 0.30 (이정표 간격 30%)
- `checkline_floor_krw`: 60000000 (고정 하한 6,000만)
- `checkline_trailing`: 0.50 (고점 대비 -50%)

## 메모
- 야후 15~20분 지연. 1년 묻기엔 무방.
- 미국 종목(SNXX/SNDU/MUU/SOXL)은 환율 곱해 원화로 합산.
- 하닉레버 티커는 `0195S0.KS` (기존 봇 설정). 안 맞으면 holdings.json에서 수정.
- 알림은 거의 안 울린다(체크선 닿을 때만). 평소엔 신호등 웹만.
