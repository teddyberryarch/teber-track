# -*- coding: utf-8 -*-
"""
teber-track — 1년 묻지마 투자 신호등 봇 (GitHub Actions cron)
- 한 번 실행 → 전체 포트 평가액 계산 → 이정표/체크선 판정 → 이벤트 시 텔레그램 → data.json/state.json 저장 → 종료
- 비용 0 (GitHub Actions). 자동주문 없음.

설계 (오늘 상의한 룰):
- 이정표: 시작 평가액에서 직전 대비 +30% 복리 (1.3^n). 도달 시 🚀 알림 + 체크선 상향.
- 체크선: max(6,000만 고정, 사상 고점 × 0.5(트레일링 -50%)). 닿으면 🔴 긴급 점검 알림.
- 신호: 🚀 이정표 도달 / 🟢 안전지대 / 🔴 체크선 근접.
- 웹에는 정확한 금액 숨김 — 위치(몇 단계)와 거리(안전/근접)만 내보냄. "애매해야 길게 간다."

환경변수 (Actions Secrets): TG_TOKEN, TG_CHAT_ID
"""

import os, json, time, math
import yfinance as yf
import requests

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

STATE_FILE    = "state.json"      # 봇 기억 (고점, 마지막 이정표, 마지막 신호)
DATA_FILE     = "data.json"       # 대시보드가 읽음 (금액 숨김, 신호만)
HOLDINGS_FILE = "holdings.json"   # 보유값 + 설정

# ── 기본값 (holdings.json 없을 때) ──────────────────────────
DEFAULT = {
    "_config": {
        "start_value_krw": 0,
        "milestone_growth": 0.30,
        "checkline_floor_krw": 60_000_000,
        "checkline_trailing": 0.50,
    },
    "holdings": {
        "hynix_lev": {"label": "하닉레버", "yf": "0195S0.KS", "ccy": "KRW", "axis": "HBM",   "shares": 1215},
        "snxx":      {"label": "SNXX",     "yf": "SNXX",      "ccy": "USD", "axis": "HBF",   "shares": 633},
        "sndu":      {"label": "SNDU",     "yf": "SNDU",      "ccy": "USD", "axis": "HBF",   "shares": 0},
        "muu":       {"label": "MUU",      "yf": "MUU",       "ccy": "USD", "axis": "HBM",   "shares": 13},
        "soxl":      {"label": "SOXL",     "yf": "SOXL",      "ccy": "USD", "axis": "LOGIC", "shares": 28},
    },
}


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TG 미설정 — 알림 생략:", text.replace("\n", " ")[:60])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as e:
        print("tg_send:", e)


def price(yticker):
    """현재가만. 실패 시 None."""
    t = yf.Ticker(yticker)
    try:
        p = float(t.fast_info["last_price"])
        if p and p == p:  # not NaN
            return p
    except Exception:
        pass
    try:
        h = t.history(period="5d", interval="1d")
        if h is not None and not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def usdkrw():
    try:
        t = yf.Ticker("KRW=X")
        try:
            return float(t.fast_info["last_price"])
        except Exception:
            pass
        h = t.history(period="5d", interval="1d")
        return float(h["Close"].iloc[-1])
    except Exception as e:
        print("usdkrw 실패, 기본값:", e)
        return 1530.0


def main():
    cfg_all = read_json(HOLDINGS_FILE, DEFAULT)
    cfg     = cfg_all.get("_config", DEFAULT["_config"])
    holds   = cfg_all.get("holdings", DEFAULT["holdings"])
    state   = read_json(STATE_FILE, {})

    growth        = cfg.get("milestone_growth", 0.30)
    floor         = cfg.get("checkline_floor_krw", 60_000_000)
    trailing      = cfg.get("checkline_trailing", 0.50)
    start_value   = cfg.get("start_value_krw", 0)

    fx = usdkrw()

    # ── 전체 포트 평가액 (원화 합산). 정확한 금액은 내부에서만 사용, 웹엔 안 내보냄 ──
    total = 0.0
    missing = []
    axis_val = {"HBM": 0.0, "HBF": 0.0, "LOGIC": 0.0}
    for name, c in holds.items():
        p = price(c["yf"])
        if p is None:
            missing.append(c["label"])
            continue
        krw = fx if c["ccy"] == "USD" else 1.0
        val = p * krw * c.get("shares", 0)
        total += val
        ax = c.get("axis", "LOGIC")
        axis_val[ax] = axis_val.get(ax, 0.0) + val

    if total <= 0:
        # 시세 전체 실패 — data.json은 건드리지 않고 종료(직전 상태 유지)
        print("시세 전체 실패 — 종료. missing:", missing)
        return

    # ── 시작 평가액 (이정표 기준점) ──
    # holdings에 start_value_krw가 0이면 최초 1회 현재 평가액을 시작점으로 고정
    if not start_value:
        start_value = state.get("start_value", 0)
        if not start_value:
            start_value = total
            state["start_value"] = start_value
            print(f"시작 평가액 자동 설정: {start_value:,.0f}원")
    else:
        state["start_value"] = start_value

    # ── 사상 고점 추적 ──
    peak = max(state.get("peak", 0), total)
    state["peak"] = peak

    # ── 이정표 단계 (시작 × 1.3^n) ──
    # 현재 total이 몇 번째 이정표를 넘었는지
    def milestone_value(n):
        return start_value * ((1 + growth) ** n)

    cur_step = 0
    while milestone_value(cur_step + 1) <= total:
        cur_step += 1
    next_ms = milestone_value(cur_step + 1)
    prev_ms = milestone_value(cur_step)

    # ── 체크선 = max(고정 하한, 고점 × (1-트레일링)) ──
    checkline = max(floor, peak * (1 - trailing))

    # ── 신호 zone 판정 ──
    # 🔴 체크선 근접: total이 체크선의 +8% 이내
    # 🟢 안전지대: 그 사이
    # (🚀는 이정표 새로 넘었을 때 이벤트로)
    near_check = total <= checkline * 1.08
    at_check   = total <= checkline
    if at_check:
        zone = "RED"
    elif near_check:
        zone = "AMBER"
    else:
        zone = "GREEN"

    # 체크선까지 거리 비율 (웹엔 "안전/근접"만, 숫자는 대략 구간만)
    dist_to_check = (total - checkline) / total if total else 0  # 0~1

    # ── 이벤트 감지 (알림) ──
    prev_step  = state.get("last_step", 0)
    prev_zone  = state.get("last_zone", "GREEN")
    events = []

    # 🚀 새 이정표 도달 (위로만)
    if cur_step > prev_step:
        events.append(("milestone", cur_step))

    # 🔴 체크선 진입 (GREEN/AMBER → RED 전이 시 1회)
    if zone == "RED" and prev_zone != "RED":
        events.append(("checkline", cur_step))
    # 🟡 체크선 근접 경고 (GREEN → AMBER 전이 시 1회)
    if zone == "AMBER" and prev_zone == "GREEN":
        events.append(("near", cur_step))

    state["last_step"] = cur_step
    state["last_zone"] = zone

    # ── 알림 발송 ──
    for kind, step in events:
        if kind == "milestone":
            tg_send("\n".join([
                f"🚀 <b>이정표 {step}단계 도달</b>",
                f"직전 대비 +30% 복리로 한 칸 전진.",
                f"체크선도 한 칸 올라갑니다 — 수익 보호.",
                "",
                "확인만 하고 다시 묻으세요. 가격은 안 봅니다.",
            ]))
        elif kind == "near":
            tg_send("\n".join([
                f"🟡 <b>체크선 근접</b>",
                f"안전지대 하단에 가까워졌습니다.",
                "",
                "아직 점검 단계는 아님. 전제(메모리 사이클) 살아있나 가볍게 확인.",
            ]))
        elif kind == "checkline":
            tg_send("\n".join([
                f"🔴 <b>체크선 도달 — 긴급 점검</b>",
                f"자동 손절 아님. 이유를 봅니다.",
                "",
                "□ 메모리 감산 발표?",
                "□ DRAM/NAND 가격 하락 전환?",
                "□ AI capex 둔화?",
                "□ 삼성 HBF 물량 장악?",
                "→ 전제 깨졌으면 정리. 일시적 패닉이면 버팀.",
                "전제가 깨졌는데 '조금만 더'는 금지.",
            ]))

    # ── data.json (웹용) — 정확한 금액 숨김. 신호/위치/거리만 ──
    # 거리는 3구간으로만: far(여유)/mid/near
    if dist_to_check > 0.30:
        dist_label = "far"
    elif dist_to_check > 0.12:
        dist_label = "mid"
    else:
        dist_label = "near"

    # 다음 이정표까지 진행률 (구간 내 위치, 숫자 아님 — 막대용 0~1)
    ms_progress = 0.0
    if next_ms > prev_ms:
        ms_progress = (total - prev_ms) / (next_ms - prev_ms)
        ms_progress = max(0.0, min(1.0, ms_progress))

    snapshot = {
        "zone": zone,                      # GREEN / AMBER / RED
        "step": cur_step,                  # 이정표 단계 (숫자)
        "dist": dist_label,                # far / mid / near (체크선까지)
        "ms_progress": round(ms_progress, 3),  # 다음 이정표까지 (막대)
        "peak_step": _peak_step(start_value, peak, growth),  # 고점이 도달했던 최고 단계
        "ts": int(time.time()),
        "fx": round(fx, 1),
        "missing": missing,
    }
    write_json(DATA_FILE, snapshot)
    write_json(STATE_FILE, state)

    print(f"total={total:,.0f} start={start_value:,.0f} peak={peak:,.0f} "
          f"step={cur_step} zone={zone} check={checkline:,.0f} dist={dist_label} ev={events}")


def _peak_step(start, peak, growth):
    if start <= 0:
        return 0
    n = 0
    while start * ((1 + growth) ** (n + 1)) <= peak:
        n += 1
    return n


if __name__ == "__main__":
    main()
