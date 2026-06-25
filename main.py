#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teber-track — 블랙잭 프로젝트 신호등 봇
- holdings.json 을 읽어 5종목 시세를 가져와 총 평가액 계산
- 고점 추적 → 이정표 / 3단 방어선 / 횡보 / 본주 -30% / heartbeat 판정
- index.html 이 읽는 data.json 생성 (현재 평가액은 일부러 미포함, 목표선 금액만)
- 위험 신호 발생 시에만 텔레그램 알림 (🟢🟡 평상시엔 조용)

환경변수(=GitHub Secrets): TG_TOKEN, TG_CHAT_ID
실행: python main.py   (data.json, state.json 을 repo 루트에 씀)
"""

import os
import json
import math
import datetime as dt
import urllib.request
import urllib.error

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_PATH = os.path.join(HERE, "holdings.json")
DATA_PATH = os.path.join(HERE, "data.json")
STATE_PATH = os.path.join(HERE, "state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ─────────────────────────────────────────────────────────
# 시세 가져오기
# ─────────────────────────────────────────────────────────
def yf_price(ticker):
    """yfinance 로 마지막 종가. 실패하면 None."""
    try:
        t = yf.Ticker(ticker)
        # 1차: fast_info
        try:
            p = float(t.fast_info["lastPrice"])
            if p and not math.isnan(p):
                return p
        except Exception:
            pass
        # 2차: 최근 종가
        hist = t.history(period="5d")
        if len(hist) > 0:
            p = float(hist["Close"].dropna().iloc[-1])
            if p and not math.isnan(p):
                return p
    except Exception as e:
        print(f"  yf fail {ticker}: {e}")
    return None


def naver_etf_price(code):
    """
    네이버 모바일 금융 API 폴백.
    하닉레버(0195S0) 처럼 야후에 아직 안 잡히는 신상 국내 ETF용.
    https://m.stock.naver.com/api/stock/{code}/basic -> {"closePrice":"25,370",...}
    """
    code = code.replace(".KS", "").replace(".KQ", "").strip()
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8"))
        raw = j.get("closePrice") or j.get("nowVal") or ""
        raw = str(raw).replace(",", "").strip()
        if raw:
            p = float(raw)
            if p and not math.isnan(p):
                return p
    except Exception as e:
        print(f"  naver fail {code}: {e}")
    return None


def get_price(yf_ticker, ccy):
    """야후 우선, 국내(.KS/.KQ)면 실패 시 네이버 폴백."""
    p = yf_price(yf_ticker)
    if p is not None:
        return p
    if ccy == "KRW" and (".KS" in yf_ticker or ".KQ" in yf_ticker):
        p = naver_etf_price(yf_ticker)
        if p is not None:
            print(f"  -> {yf_ticker} 네이버 폴백 성공: {p}")
            return p
    return None


def usdkrw():
    """USD->KRW 환율. 실패 시 보수적 기본값."""
    for tk in ("KRW=X", "USDKRW=X"):
        p = yf_price(tk)
        if p and p > 500:   # sanity
            return p
    print("  환율 실패 -> 기본값 1500 사용")
    return 1500.0


# ─────────────────────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("  [TG 미설정] " + msg.replace("\n", " "))
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID, "text": msg,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"  TG send fail: {e}")


# ─────────────────────────────────────────────────────────
# 상태 저장
# ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def won(n):
    if n >= 1e8:
        return f"{round(n/1e8, 2)}억"
    if n >= 1e4:
        return f"{round(n/1e4):,}만"
    return f"{round(n):,}원"


ZONE_RANK = {"GREEN": 0, "SIDEWAYS": 0, "AMBER": 1, "ORANGE": 2, "RED": 3}


def main():
    today = dt.date.today().isoformat()
    with open(HOLDINGS_PATH, encoding="utf-8") as f:
        H = json.load(f)
    cfg = H["_config"]
    state = load_state()

    # ── 1. 시세 ─────────────────────────────
    fx = usdkrw()
    total_krw = 0.0
    missing = []
    out_holdings = {}
    for key, h in H["holdings"].items():
        price = get_price(h["yf"], h["ccy"])
        out_holdings[key] = {"label": h["label"], "axis": h["axis"], "shares": h["shares"]}
        if price is None:
            missing.append(h["label"])
            print(f"MISSING {h['label']} ({h['yf']})")
            continue
        val = price * h["shares"] * (fx if h["ccy"] == "USD" else 1.0)
        total_krw += val
        print(f"OK {h['label']:12s} {price:>14,.2f} {h['ccy']} x{h['shares']} = {won(val)}")

    # ── 2. 고점 추적 ─────────────────────────
    start = cfg["start_value_krw"]
    peak = max(state.get("peak_krw", start), total_krw) if total_krw > 0 else state.get("peak_krw", start)
    peak_changed = peak > state.get("peak_krw", start) + 1
    if peak_changed or "peak_date" not in state:
        state["peak_date"] = today
        state["runs_since_peak"] = 0
    state["peak_krw"] = peak

    # ── 3. 이정표 (peak 기준) ────────────────
    g = cfg["milestone_growth"]
    step = 0
    if peak > start:
        step = int(math.floor(math.log(peak / start) / math.log(1 + g)))
        step = max(0, step)
    cur_floor = start * (1 + g) ** step
    next_ms = start * (1 + g) ** (step + 1)
    ms_progress = 0.0
    if next_ms > cur_floor:
        ms_progress = max(0.0, min(1.0, (peak - cur_floor) / (next_ms - cur_floor)))
    peak_step = max(step, state.get("peak_step", 0))
    state["peak_step"] = peak_step

    # ── 4. 3단 방어선 (peak 대비) ────────────
    guard1 = peak * (1 - cfg["guard1_drop"])
    guard2 = peak * (1 - cfg["guard2_drop"])
    guard3 = peak * (1 - cfg["guard3_drop"])
    floor = cfg["checkline_floor_krw"]

    # ── 5. zone 판정 ─────────────────────────
    has_price = total_krw > 0 and len(missing) < len(H["holdings"])
    if not has_price:
        zone = state.get("last_zone", "GREEN")   # 시세 다 빠지면 직전 유지
    elif total_krw <= guard3 or total_krw <= floor:
        zone = "RED"
    elif total_krw <= guard2:
        zone = "ORANGE"
    elif total_krw <= guard1:
        zone = "AMBER"
    else:
        zone = "GREEN"

    # ── 6. 횡보 (고점 근처서 60거래일 정체) ──
    runs = state.get("runs_since_peak", 0) + 1
    state["runs_since_peak"] = runs
    rpd = 2  # cron 하루 2회 가정
    trading_days = runs / rpd
    sideways_ratio = min(2.0, trading_days / cfg["sideways_days"])
    near_peak = has_price and total_krw >= peak * cfg["sideways_band"]
    if zone == "GREEN" and near_peak and trading_days >= cfg["sideways_days"]:
        zone = "SIDEWAYS"
    days_since_peak = int(round(trading_days))

    # ── 7. 본주 -30% 감시 (화면 X, 알림만) ───
    und_peaks = state.get("underlying_peaks", {})
    und_flagged = set(state.get("underlying_flagged", []))
    for uk, u in H.get("underlyings", {}).items():
        up = yf_price(u["yf"])
        if up is None:
            continue
        prev = und_peaks.get(uk, up)
        upeak = max(prev, up)
        und_peaks[uk] = upeak
        drop = (upeak - up) / upeak if upeak > 0 else 0
        if drop >= cfg["underlying_peak_drop"] and uk not in und_flagged:
            und_flagged.add(uk)
            tg(f"🔴 <b>본주 급락</b>\n{u['label']} 고점 대비 -{round(drop*100)}%\n"
               f"레버는 결과·본주가 원인. 전제(슈퍼사이클) 깨졌나 점검.\n(등락률은 평소 안 봄 — 이건 알림만)")
        elif drop < cfg["underlying_peak_drop"] * 0.8 and uk in und_flagged:
            und_flagged.discard(uk)   # -24% 아래로 회복하면 플래그 해제
    state["underlying_peaks"] = und_peaks
    state["underlying_flagged"] = sorted(und_flagged)

    # 본주 백스톱 표시용 (임계치 + 종목별 발동/대기 — 실시간 등락률은 안 내보냄)
    und_drop_pct = round(cfg["underlying_peak_drop"] * 100)
    und_watch = [{"label": u["label"], "flagged": uk in und_flagged}
                 for uk, u in H.get("underlyings", {}).items()]

    # ── 8. data.json (현재 평가액 미포함) ────
    data = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "zone": zone,
        "step": step,
        "peak_step": peak_step,
        "ms_progress": round(ms_progress, 4),
        "next_ms_krw": round(next_ms),
        "guard1_krw": round(guard1),
        "guard2_krw": round(guard2),
        "guard3_krw": round(guard3),
        "sideways_ratio": round(sideways_ratio, 3),
        "sideways_months": 3,
        "days_since_peak": days_since_peak,
        "holdings": out_holdings,
        "underlying_drop_pct": und_drop_pct,
        "underlying_watch": und_watch,
        "missing": missing,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 9. 알림 (zone 악화 / 이정표 / heartbeat) ──
    last_zone = state.get("last_zone", "GREEN")
    if has_price and ZONE_RANK.get(zone, 0) > ZONE_RANK.get(last_zone, 0):
        names = {"AMBER": "🟡 1차 방어선 -25%", "ORANGE": "🟠 2차 방어선 -40%",
                 "RED": "🔴 3차 방어선 -50% · 결단", "SIDEWAYS": "🟡 횡보 — decay 점검"}
        tg(f"{names.get(zone, zone)}\n점검 ≠ 매도. 전제 깨졌나만 확인.\n"
           f"방어선: {won(guard1)} / {won(guard2)} / {won(guard3)}")
    if has_price and zone == "SIDEWAYS" and last_zone != "SIDEWAYS":
        tg(f"🟡 <b>횡보 감지</b>\n고점 후 약 {days_since_peak}거래일 정체. 추세 살아있나 점검.")
    state["last_zone"] = zone

    # 이정표 갱신 알림
    if peak_step > state.get("last_alerted_step", 0):
        state["last_alerted_step"] = peak_step
        tg(f"🚀 <b>{peak_step}단계 이정표 도달</b>\n방어선도 상향됨. 다음: {won(next_ms)}")

    # heartbeat (N회마다 1회)
    rc = state.get("run_count", 0) + 1
    state["run_count"] = rc
    if rc % cfg["heartbeat_every_runs"] == 0:
        miss_txt = f" · 시세누락: {', '.join(missing)}" if missing else ""
        tg(f"🤖 봇 정상 (heartbeat){miss_txt}\nzone={zone} · {peak_step}단계")

    save_state(state)

    # ── 10. 로그 (Run 직후 이 줄 확인) ───────
    print("=" * 50)
    print(f"total={won(total_krw)} ({round(total_krw):,}) | zone={zone} | "
          f"step={step}/{peak_step} | missing={missing} | fx={fx:.1f}")
    print("=" * 50)


if __name__ == "__main__":
    main()
