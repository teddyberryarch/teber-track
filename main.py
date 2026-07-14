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


def yf_recent_high(ticker, period="1mo"):
    """최근 1개월 장중 고가. 본주 고점 추적을 종가가 아닌 고가 기준으로 (임계 스침 방지)."""
    try:
        hist = yf.Ticker(ticker).history(period=period)
        if len(hist) > 0:
            h = float(hist["High"].dropna().max())
            if h and not math.isnan(h):
                return h
    except Exception as e:
        print(f"  yf high fail {ticker}: {e}")
    return None


def realized_vol(ticker="000660.KS", days=60):
    """하이닉스 60거래일 실현 변동성(연율화 %). 레버 손익분기(≈58%)와 비교용. 실패 시 None."""
    try:
        hist = yf.Ticker(ticker).history(period="6mo")["Close"].dropna()
        if len(hist) < 11:
            return None
        days = min(days, len(hist) - 1)
        closes = list(hist)[-(days + 1):]
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var) * math.sqrt(252) * 100
    except Exception as e:
        print(f"  vol fail {ticker}: {e}")
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

# ══ v3 패치: 봉인 히스토리 / ADR 괴리율 / AUM 감시 ══════════
HISTORY_PATH = os.path.join(HERE, "history.csv")

def day_low_high(ticker):
    """당일(최근 거래일) 저가·고가. 실패 시 (None, None)."""
    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if len(hist) > 0:
            lo = float(hist["Low"].iloc[-1]); hi = float(hist["High"].iloc[-1])
            if not (math.isnan(lo) or math.isnan(hi)):
                return lo, hi
    except Exception as e:
        print(f"  range fail {ticker}: {e}")
    return None, None

def record_history(today, prices, fx, total_krw, zone):
    """매일 종가 스냅샷을 append. 대시보드엔 안 보임 — 개봉일(2027.6.19)에 차트용.
    같은 날짜 중복 실행 시 마지막 값으로 갱신."""
    import csv
    cols = ["date", "hynix_lev", "muu", "snxx", "sndu",
            "hynix_ud", "micron_ud", "sandisk_ud", "usdkrw", "total_krw", "zone"]
    rows = {}
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = r
    rows[today] = {"date": today, **{k: (f"{v:.2f}" if isinstance(v, float) else str(v))
                                     for k, v in prices.items()},
                   "usdkrw": f"{fx:.2f}", "total_krw": f"{total_krw:.0f}", "zone": zone}
    with open(HISTORY_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in sorted(rows):
            w.writerow({c: rows[d].get(c, "") for c in cols})


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

# 방어선 = 매도선이 아니라 검증선. 도달 시 아래 4문항만 확인한다.
AUDIT_CHECKLIST = (
    "\n\n📋 <b>감사 발동 — 매도 아님, 검증임</b>\n"
    "① DRAM·NAND 계약가 QoQ — 상승/보합/하락?\n"
    "② 공급사 재고 주수 — 줄고 있나 늘고 있나?\n"
    "③ 빅4 캐펙스 가이던스 — 유지·상향 vs 하향?\n"
    "④ 하닉·마이크론 forward EPS — 상향 중 vs 하향 전환?\n"
    "→ 4개 전부 초록: 아무것도 안 한다. 계좌도 안 연다.\n"
    "→ 1개라도 빨강: 매도규칙(EPS 하향/캐펙스 축소) 검토 개시."
)


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

    # ── 5.5 봉인 히스토리 기록 (대시보드 미표시) ──
    try:
        _prices = {}
        for _k in ["hynix_lev", "muu", "snxx", "sndu"]:
            _h = H["holdings"].get(_k)
            _p = get_price(_h["yf"], _h["ccy"]) if _h else None
            _prices[_k] = _p if _p else ""
        for _k, _u in [("hynix_ud", "000660.KS"), ("micron_ud", "MU"), ("sandisk_ud", "SNDK")]:
            _p = yf_price(_u)
            _prices[_k] = _p if _p else ""
        record_history(today, _prices, fx, total_krw, zone)
        # 당일 레인지: 각 종목 저가·고가 조합 → 포트 총액 밴드
        _lo_sum, _hi_sum, _rng_ok = 0.0, 0.0, True
        for _k, _h in H["holdings"].items():
            _lo, _hi = day_low_high(_h["yf"])
            if _lo is None:
                _rng_ok = False; break
            _m = fx if _h["ccy"] == "USD" else 1.0
            _lo_sum += _lo * _h["shares"] * _m
            _hi_sum += _hi * _h["shares"] * _m
        if _rng_ok and _lo_sum > 0:
            state["day_range_krw"] = [round(_lo_sum), round(_hi_sum)]
    except Exception as _e:
        print("history/adr/aum skip:", _e)

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
        uhigh = yf_recent_high(u["yf"])
        prev = und_peaks.get(uk, up)
        upeak = max([prev, up] + ([uhigh] if uhigh else []))
        und_peaks[uk] = upeak
        drop = (upeak - up) / upeak if upeak > 0 else 0
        if drop >= cfg["underlying_peak_drop"] and uk not in und_flagged:
            und_flagged.add(uk)
            tg(f"🔴 <b>본주 급락</b>\n{u['label']} 고점 대비 -{round(drop*100)}%\n"
               f"레버 등락의 원인은 본주. 전제(슈퍼사이클) 깨졌나만 점검.\n"
               f"(등락률은 평소 안 봄 — 이건 알림만. 매도 신호 아님)")
        elif drop < cfg["underlying_peak_drop"] * 0.8 and uk in und_flagged:
            und_flagged.discard(uk)   # -24% 아래로 회복하면 플래그 해제
    state["underlying_peaks"] = und_peaks
    state["underlying_flagged"] = sorted(und_flagged)

    # 본주 백스톱 표시용 (임계치 + 종목별 발동/대기 — 실시간 등락률은 안 내보냄)
    und_drop_pct = round(cfg["underlying_peak_drop"] * 100)
    und_watch = [{"label": u["label"], "flagged": uk in und_flagged}
                 for uk, u in H.get("underlyings", {}).items()]

    # ── 8. 변동성 게이지 (하닉 60거래일 실현 σ, 레버 분기점 58%와 비교) ──
    vol = realized_vol()
    if vol:
        print(f"  σ60d(하닉) = {vol:.1f}% (레버 분기점 ~58%)")

    # ── 8-1. data.json (현재 평가액 미포함) ────
    data = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "zone": zone,
        "day_range_krw": state.get("day_range_krw"),
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
        "vol_pct": round(vol, 1) if vol else None,
        "vol_breakeven_pct": 58,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 9. 알림 (zone 악화 / 이정표 / heartbeat) ──
    last_zone = state.get("last_zone", "GREEN")
    if has_price and ZONE_RANK.get(zone, 0) > ZONE_RANK.get(last_zone, 0):
        names = {"AMBER": "🟡 1차 방어선 −25% 하회", "ORANGE": "🟠 2차 방어선 −40% 하회",
                 "RED": "🔴 3차 방어선 −50% 하회", "SIDEWAYS": "🟡 횡보 — decay 점검"}
        base = (f"{names.get(zone, zone)}\n가격 경보 ≠ 매도 신호. 방어선의 기능은 검증 강제까지.\n"
                f"방어선: {won(guard1)} / {won(guard2)} / {won(guard3)}")
        base += AUDIT_CHECKLIST
        if zone == "RED":
            base += ("\n\n🔴 <b>−50% 특칙 — 시한부 검증</b>\n"
                     "여기서부턴 가격 자체가 약한 증거(펀더멘털 멀쩡한데 반토막은 드묾).\n"
                     "체크리스트 전부 초록이어도: 다음 분기 실적 발표를 데드라인으로 지정.\n"
                     "가이던스 꺾이면 → 봉인 해제 검토 (청산 대신 <b>본주 1배 전환</b> 우선).\n"
                     "안 꺾이면 → 홀드 지속. '가격이 틀렸다'에 무기한 베팅하지 않는다.")
        tg(base)
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
        vol_txt = f"\nσ(하닉 60d) {vol:.0f}% — 레버 분기점 58% ({'위험권' if vol >= 58 else '우호권'})" if vol else ""
        tg(f"🤖 봇 정상 (heartbeat){miss_txt}\nzone={zone} · {peak_step}단계{vol_txt}\n(봉인 히스토리 기록 중)")

    save_state(state)

    # ── 10. 로그 (Run 직후 이 줄 확인) ───────
    print("=" * 50)
    print(f"total={won(total_krw)} ({round(total_krw):,}) | zone={zone} | "
          f"step={step}/{peak_step} | missing={missing} | fx={fx:.1f}")
    print("=" * 50)


if __name__ == "__main__":
    main()
