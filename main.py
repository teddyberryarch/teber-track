# -*- coding: utf-8 -*-
"""
teber-track — 1년 묻지마 투자 신호등 봇 (GitHub Actions cron)
- 전체 포트 평가액 계산 → 다단계 방어선/이정표/횡보 판정 → 이벤트 시 텔레그램 → data.json/state.json 저장 → 종료
- 비용 0 (GitHub Actions). 자동주문 없음.

핵심 설계 (상의한 룰 전부):
- 이정표: 시작 평가액에서 직전 대비 +30% 복리(1.3^n). 도달 시 🚀 + 방어선 상향.
- 다단계 방어선 (고점 대비): 한 번 놓쳐도 또 알림 → "1년 만에 봤더니 10%" 방지
    · -25% → 🟡 주의 (빠지기 시작)
    · -40% → 🟠 경고 (추세 의심, 진지하게)
    · -50% → 🔴 결단 (회복 불가 직전, 반드시 점검 / 안 보기 예외)
  + 원금 기준 고정 하한(6,000만)도 병행 (천천히 녹는 것 방어)
- 횡보(decay): 고점 후 일정 기간(기본 60거래일) 정체 → 🟡 횡보 점검(조용히 녹는 것)
- heartbeat: 주 1회 "봇 정상" 한 줄 → 침묵이 고장인지 안전인지 구분
- 목표선 금액(이정표·방어선)은 표시(고정값). 현재 평가액(total)은 숨김.

환경변수 (Actions Secrets): TG_TOKEN, TG_CHAT_ID
"""

import os, json, time
import yfinance as yf
import requests

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

STATE_FILE    = "state.json"
DATA_FILE     = "data.json"
HOLDINGS_FILE = "holdings.json"

DEFAULT = {
    "_config": {
        "start_value_krw": 0,
        "milestone_growth": 0.30,
        "checkline_floor_krw": 60000000,
        "guard1_drop": 0.25,
        "guard2_drop": 0.40,
        "guard3_drop": 0.50,
        "sideways_days": 60,
        "sideways_band": 0.92,
        "heartbeat_every_runs": 14,
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
            "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as e:
        print("tg_send:", e)


def price(yticker):
    t = yf.Ticker(yticker)
    try:
        p = float(t.fast_info["last_price"])
        if p and p == p:
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


def milestone_value(start, n, growth):
    return start * ((1 + growth) ** n)


def peak_step(start, peak, growth):
    if start <= 0:
        return 0
    n = 0
    while start * ((1 + growth) ** (n + 1)) <= peak:
        n += 1
    return n


def main():
    cfg_all = read_json(HOLDINGS_FILE, DEFAULT)
    cfg     = cfg_all.get("_config", DEFAULT["_config"])
    holds   = cfg_all.get("holdings", DEFAULT["holdings"])
    state   = read_json(STATE_FILE, {})

    growth        = cfg.get("milestone_growth", 0.30)
    floor         = cfg.get("checkline_floor_krw", 60000000)
    g1            = cfg.get("guard1_drop", 0.25)
    g2            = cfg.get("guard2_drop", 0.40)
    g3            = cfg.get("guard3_drop", 0.50)
    start_value   = cfg.get("start_value_krw", 0)
    sideways_days = cfg.get("sideways_days", 60)
    sideways_band = cfg.get("sideways_band", 0.92)
    hb_every      = cfg.get("heartbeat_every_runs", 14)

    fx = usdkrw()

    total = 0.0
    missing = []
    for name, c in holds.items():
        p = price(c["yf"])
        if p is None:
            missing.append(c["label"])
            continue
        krw = fx if c["ccy"] == "USD" else 1.0
        total += p * krw * c.get("shares", 0)

    if total <= 0:
        print("시세 전체 실패 — 종료. missing:", missing)
        return

    if not start_value:
        start_value = state.get("start_value", 0)
        if not start_value:
            start_value = total
            state["start_value"] = start_value
            print("시작 평가액 자동 설정: %d" % round(start_value))
    else:
        state["start_value"] = start_value

    run_count = state.get("run_count", 0) + 1
    state["run_count"] = run_count

    prev_peak = state.get("peak", 0)
    peak = max(prev_peak, total)
    state["peak"] = peak
    if total >= prev_peak:
        state["peak_run"] = run_count
    peak_run = state.get("peak_run", run_count)

    cur_step = 0
    while milestone_value(start_value, cur_step + 1, growth) <= total:
        cur_step += 1
    next_ms = milestone_value(start_value, cur_step + 1, growth)
    prev_ms = milestone_value(start_value, cur_step, growth)

    # ── 다단계 방어선 (고점 대비) ──
    guard1 = peak * (1 - g1)
    guard2 = peak * (1 - g2)
    guard3_trail = peak * (1 - g3)
    guard3 = max(floor, guard3_trail)   # 3차는 원금 하한과 병행

    # 고점 대비 현재 낙폭
    dd_from_peak = (total - peak) / peak if peak else 0   # 음수

    # ── 횡보(decay) 감지 ──
    runs_per_day = 2
    sideways_runs = sideways_days * runs_per_day
    runs_since_peak = run_count - peak_run
    near_peak = total >= peak * sideways_band
    stalled   = runs_since_peak >= sideways_runs
    is_sideways = near_peak and stalled and (total > guard1)

    # ── zone 판정 (우선순위: RED > ORANGE > AMBER > SIDEWAYS > GREEN) ──
    if total <= guard3:
        zone = "RED"
    elif total <= guard2:
        zone = "ORANGE"
    elif total <= guard1:
        zone = "AMBER"
    elif is_sideways:
        zone = "SIDEWAYS"
    else:
        zone = "GREEN"

    # ── 이벤트 감지 ──
    prev_step = state.get("last_step", 0)
    prev_zone = state.get("last_zone", "GREEN")
    events = []

    if cur_step > prev_step:
        events.append("milestone")
    # 방어선은 "더 나쁜 단계로 처음 진입할 때"만 알림 (반복 안 함)
    zone_rank = {"GREEN": 0, "SIDEWAYS": 0, "AMBER": 1, "ORANGE": 2, "RED": 3}
    if zone_rank.get(zone, 0) > zone_rank.get(prev_zone, 0):
        if zone == "AMBER":
            events.append("guard1")
        elif zone == "ORANGE":
            events.append("guard2")
        elif zone == "RED":
            events.append("guard3")
    if zone == "SIDEWAYS" and prev_zone != "SIDEWAYS":
        events.append("sideways")

    state["last_step"] = cur_step
    state["last_zone"] = zone

    # ── heartbeat (주 1회 봇 생존 신호) ──
    last_hb = state.get("last_heartbeat_run", 0)
    do_hb = (run_count - last_hb) >= hb_every
    if do_hb and not events:   # 다른 알림 있으면 굳이 또 안 보냄
        state["last_heartbeat_run"] = run_count

    # ── 알림 발송 ──
    for kind in events:
        if kind == "milestone":
            tg_send("\n".join([
                "🚀 <b>이정표 %d단계 도달</b>" % cur_step,
                "직전 대비 +30% 복리로 한 칸 전진.",
                "방어선도 한 칸 올라갑니다 — 수익 보호.",
                "",
                "확인만 하고 다시 묻으세요. 가격은 안 봅니다.",
            ]))
        elif kind == "guard1":
            tg_send("\n".join([
                "🟡 <b>1차 방어선 — 고점 대비 -25%</b>",
                "빠지기 시작했습니다. 아직 여유.",
                "",
                "전제(메모리 사이클) 살아있나 가볍게 확인만.",
                "조정(-20~40%)은 예상된 것. 던지지 않습니다.",
            ]))
        elif kind == "guard2":
            tg_send("\n".join([
                "🟠 <b>2차 방어선 — 고점 대비 -40%</b>",
                "추세를 의심해야 합니다. 진지하게.",
                "",
                "□ 메모리 감산? □ DRAM/NAND 가격 하락? □ AI capex 둔화?",
                "전제 깨졌으면 정리 준비. 일시적이면 버팀.",
                "다음 단계(-50%)는 회복 불가 직전입니다.",
            ]))
        elif kind == "guard3":
            tg_send("\n".join([
                "🔴🔴 <b>3차 방어선 — 고점 대비 -50% · 결단</b>",
                "⚠️ 이건 '안 보기' 예외입니다. 반드시 확인하세요.",
                "",
                "레버는 여기서 더 빠지면 회복이 거의 불가능합니다.",
                "(-50%→본전은 +100%, -70%→본전은 +233% 필요)",
                "",
                "□ 전제가 진짜 깨졌나? (감산·가격하락·capex둔화·삼성HBF)",
                "→ 깨졌으면 즉시 정리. 일시적 패닉이면 버팀.",
                "'전제 깨졌는데 조금만 더'는 절대 금지.",
            ]))
        elif kind == "sideways":
            months = round(sideways_days / 21)
            tg_send("\n".join([
                "🟡 <b>횡보 %d개월+ — decay 점검</b>" % months,
                "급락은 아닌데 고점 부근에서 오래 정체 중.",
                "레버는 횡보장에서 조용히 녹습니다(연 3~17%).",
                "",
                "□ 추세 살아있나, 꺾여서 횡보인가?",
                "일시적 숨고르기면 묻기. 추세 죽었으면 판단.",
            ]))

    if do_hb and not events:
        face = {"GREEN":"🟢","SIDEWAYS":"🟡","AMBER":"🟡","ORANGE":"🟠","RED":"🔴"}.get(zone, "🟢")
        tg_send("\n".join([
            "%s <b>봇 정상 작동 중</b> (주간 점검)" % face,
            "현재 상태: %s · %d단계" % (zone, cur_step),
            "",
            "이 메시지가 안 오면 봇이 멈춘 것 → 확인 필요.",
            "평소엔 안 봐도 됩니다. 위험할 때만 따로 알립니다.",
        ]))

    # ── data.json ──
    # 방어선까지 거리 (가장 가까운 미발동 방어선 기준)
    if total > guard1:
        dist_label = "far"; next_guard = guard1; next_guard_name = "1차(-25%)"
    elif total > guard2:
        dist_label = "mid"; next_guard = guard2; next_guard_name = "2차(-40%)"
    elif total > guard3:
        dist_label = "near"; next_guard = guard3; next_guard_name = "3차(-50%)"
    else:
        dist_label = "near"; next_guard = guard3; next_guard_name = "방어선 도달"

    ms_progress = 0.0
    if next_ms > prev_ms:
        ms_progress = (total - prev_ms) / (next_ms - prev_ms)
        ms_progress = max(0.0, min(1.0, ms_progress))

    sideways_ratio = 0.0
    if sideways_runs > 0:
        sideways_ratio = min(1.0, runs_since_peak / sideways_runs)
    days_since_peak = round(runs_since_peak / runs_per_day)

    snapshot = {
        "zone": zone,
        "step": cur_step,
        "dist": dist_label,
        "ms_progress": round(ms_progress, 3),
        "peak_step": peak_step(start_value, peak, growth),
        "next_ms_krw": round(next_ms),
        "this_ms_krw": round(prev_ms),
        # 3단 방어선 금액 (고정 목표선, 표시 OK)
        "guard1_krw": round(guard1),
        "guard2_krw": round(guard2),
        "guard3_krw": round(guard3),
        "next_guard_krw": round(next_guard),
        "next_guard_name": next_guard_name,
        "floor_krw": round(floor),
        "start_krw": round(start_value),
        "dd_from_peak": round(dd_from_peak, 3),
        "sideways_ratio": round(sideways_ratio, 2),
        "days_since_peak": days_since_peak,
        "sideways_months": round(sideways_days / 21),
        "holdings": {k: {"label": c["label"], "shares": c.get("shares", 0), "axis": c.get("axis", "")}
                     for k, c in holds.items()},
        "ts": int(time.time()),
        "fx": round(fx, 1),
        "missing": missing,
    }
    write_json(DATA_FILE, snapshot)
    write_json(STATE_FILE, state)

    print("total=%d start=%d peak=%d step=%d zone=%s g1=%d g2=%d g3=%d since_peak=%dd ev=%s missing=%s" % (
        round(total), round(start_value), round(peak), cur_step, zone,
        round(guard1), round(guard2), round(guard3), days_since_peak, events, missing))


if __name__ == "__main__":
    main()
