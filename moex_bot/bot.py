#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOEX Scanner Bot — следит за акциями Мосбиржи и присылает в Telegram сценарий сделки,
когда находит одну из ситуаций:
  1) сильная просадка при росте объёма (с поправкой на дивидендный гэп);
  2) технические сигналы: RSI < 30, возврат RSI выше 30, пробой 20-дневного максимума
     на объёме, пересечение SMA20 и SMA50 снизу вверх.

Режимы:
  python bot.py run                 — бот работает постоянно: команды в Telegram + скан по будням
  python bot.py scan [--force]      — один скан (для cron); --force игнорирует паузу между алертами
  python bot.py check SBER          — показать метрики и сигналы по одной бумаге
  python bot.py backtest [SBER ...] — проверить правила на истории (по умолчанию весь список)

Это информационный инструмент, а не инвестиционная рекомендация.
"""
import os
import re
import sys
import json
import math
import time
import html
import logging
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# ============================== НАСТРОЙКИ ==============================
C = dict(
    history_days=420,        # сколько календарных дней истории грузить для скана
    min_turnover_rub=100e6,  # фильтр ликвидности: средний дневной оборот за 20 дн., ₽
    # --- просадка + объём ---
    drop_lookback=10,        # окно (торговых дней) для максимума закрытия
    drop_pct=10.0,           # минимальная просадка от максимума, %
    vol_window=20,           # окно среднего объёма
    vol_mult=1.5,            # объём сегодня / средний >= этого значения
    # --- технические сигналы ---
    rsi_period=14,
    rsi_oversold=30,
    breakout_window=20,
    breakout_vol_mult=1.3,
    sma_fast=20,
    sma_slow=50,
    atr_period=14,
    # --- фильтр качества сигнала ---
    min_rr=1.5,              # минимальное соотношение прибыль/риск до цели 1
    cooldown_days=3,         # не слать алерт по той же бумаге чаще, чем раз в N дней
    # --- бэктест ---
    hold_days=20,            # максимум дней в сделке
    cost_pct=0.15,           # комиссии и проскальзывание на круг, %
    backtest_days=1100,      # календарных дней истории для бэктеста
    # --- расписание ---
    scan_hour=19,            # время ежедневного скана (МСК), по будням
    scan_minute=0,
    daily_summary=True,      # присылать короткий итог, даже если сигналов нет
)

DEFAULT_WATCHLIST = [
    "SBER", "LKOH", "X5", "YDEX", "GAZP", "OZON", "T", "POSI",
    "ROSN", "NVTK", "TATN", "SNGS", "GMKN", "PLZL", "MTSS", "MGNT", "MOEX",
    "VTBR", "ALRS", "CHMF", "NLMK", "AFLT", "PIKK", "SMLT", "IRAO", "RUAL", "PHOR",
]
NAMES = {
    "SBER": "Сбербанк", "LKOH": "ЛУКОЙЛ", "X5": "X5 Group", "YDEX": "Яндекс", "GAZP": "Газпром",
    "OZON": "Ozon", "T": "Т-Технологии", "POSI": "Группа Позитив", "ROSN": "Роснефть",
    "NVTK": "Новатэк", "TATN": "Татнефть", "SNGS": "Сургутнефтегаз", "GMKN": "Норникель",
    "PLZL": "Полюс", "MTSS": "МТС", "MGNT": "Магнит", "MOEX": "Мосбиржа", "VTBR": "ВТБ",
    "ALRS": "АЛРОСА", "CHMF": "Северсталь", "NLMK": "НЛМК", "AFLT": "Аэрофлот",
    "PIKK": "ПИК", "SMLT": "Самолёт", "IRAO": "Интер РАО", "RUAL": "РУСАЛ", "PHOR": "ФосАгро",
}
# =======================================================================

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
MSK = ZoneInfo("Europe/Moscow")
ISS = "https://iss.moex.com/iss"
log = logging.getLogger("moexbot")


def _load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
CAPITAL = float(os.getenv("CAPITAL_RUB", "0") or 0)     # необязательно: ваш капитал для оценки объёма
RISK_PCT = float(os.getenv("RISK_PCT", "1") or 1)        # риск на сделку, % капитала
MAX_POS_PCT = float(os.getenv("MAX_POS_PCT", "20") or 20)  # максимум одной позиции, % капитала


# ============================== ДАННЫЕ MOEX ==============================
def iss_get(url, params=None):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def fetch_candles(secid, days):
    """Дневные свечи акции на основной доске TQBR."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url = f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/{secid}/candles.json"
    rows, cols, offset = [], None, 0
    while True:
        j = iss_get(url, {"from": start, "interval": 24, "start": offset})["candles"]
        cols = j["columns"]
        data = j["data"]
        if not data:
            break
        rows += data
        offset += len(data)
        if len(data) < 500:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=cols)
    df["begin"] = pd.to_datetime(df["begin"])
    df = df.rename(columns={"begin": "date"}).set_index("date")
    df = df[["open", "high", "low", "close", "volume", "value"]].astype(float)
    return df[df["volume"] > 0]


def fetch_dividends(secid):
    """Список (дата закрытия реестра, дивиденд на акцию)."""
    j = iss_get(f"{ISS}/securities/{secid}/dividends.json").get("dividends", {})
    cols, out = j.get("columns", []), []
    for row in j.get("data", []):
        rec = dict(zip(cols, row))
        try:
            out.append((pd.Timestamp(rec["registryclosedate"]), float(rec["value"])))
        except Exception:
            continue
    return out


def div_in_window(divs, ts, back=14, fwd=4):
    """Сумма дивидендов, чья отсечка могла вызвать падение в окне вокруг даты ts."""
    if not divs:
        return 0.0
    lo, hi = ts - pd.Timedelta(days=back), ts + pd.Timedelta(days=fwd)
    return sum(v for d, v in divs if lo <= d <= hi)


# ============================== ИНДИКАТОРЫ ==============================
def add_indicators(df):
    df = df.copy()
    c = df["close"]
    n = C["rsi_period"]
    d = c.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    df["rsi"] = 100 - 100 / (1 + au / ad)
    pc = c.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / C["atr_period"], adjust=False, min_periods=C["atr_period"]).mean()
    df["vol_avg"] = df["volume"].rolling(C["vol_window"]).mean().shift(1)
    df["vol_ratio"] = df["volume"] / df["vol_avg"]
    df["turnover_avg"] = df["value"].rolling(20).mean()
    df["high_n"] = c.rolling(C["drop_lookback"]).max()
    df["drop_pct"] = (c / df["high_n"] - 1) * 100
    df["sma_fast"] = c.rolling(C["sma_fast"]).mean()
    df["sma_slow"] = c.rolling(C["sma_slow"]).mean()
    df["brk_level"] = df["high"].rolling(C["breakout_window"]).max().shift(1)
    df["low_n"] = df["low"].rolling(C["breakout_window"]).min().shift(1)
    return df


# ============================== СИГНАЛЫ ==============================
def px(x):
    return f"{x:.2f}" if x < 100 else (f"{x:.1f}" if x < 1000 else f"{x:.0f}")


def _plan(entry, stop, t1, t2):
    risk = entry - stop
    if risk <= 0 or t1 <= entry:
        return None
    t2 = max(t2, t1)
    return dict(entry=entry, stop=stop, t1=t1, t2=t2, risk_pct=risk / entry * 100,
                rr1=(t1 - entry) / risk, rr2=(t2 - entry) / risk)


def signals_at(d, i, divs=None):
    """Сигналы на свече с позиционным индексом i (d — датафрейм с индикаторами)."""
    if i < 60:
        return []
    r, p = d.iloc[i], d.iloc[i - 1]
    need = (r.rsi, p.rsi, r.atr, r.vol_ratio, r.sma_slow, r.drop_pct, r.turnover_avg, r.brk_level, r.low_n)
    if any(pd.isna(x) for x in need) or r.turnover_avg < C["min_turnover_rub"]:
        return []
    atr = r.atr
    confirmed = bool(r.close > r.open and r.close > p.close)
    entry_soft = r.close if confirmed else r.high
    low3 = d["low"].iloc[i - 2:i + 1].min()
    low5 = d["low"].iloc[i - 4:i + 1].min()
    out = []

    # 1) просадка + рост объёма (поправка на дивидендный гэп)
    div = div_in_window(divs, d.index[i])
    adj_drop = ((r.close + div) / r.high_n - 1) * 100
    if adj_drop <= -C["drop_pct"] and r.vol_ratio >= C["vol_mult"]:
        gap = r.high_n - entry_soft
        plan = _plan(entry_soft, low3 - 0.5 * atr, entry_soft + 0.382 * gap, entry_soft + 0.618 * gap) if gap > 0 else None
        txt = (f"Просадка {r.drop_pct:.1f}% от максимума закрытия за {C['drop_lookback']} дн. "
               f"при объёме ×{r.vol_ratio:.1f} от среднего")
        if div > 0:
            txt += f" (без учёта дивидендного гэпа: {adj_drop:.1f}%)"
        out.append(dict(kind="DROP_VOL", text=txt, confirmed=confirmed, plan=plan))

    # 2) RSI в зоне перепроданности
    t_low = C["rsi_oversold"]

    def mean_rev_plan(entry, stop):
        t1 = r.sma_fast if r.sma_fast > entry else entry + 1.5 * atr
        t2 = r.sma_slow if r.sma_slow > t1 else t1 + 1.5 * atr
        return _plan(entry, stop, t1, t2)

    if r.rsi < t_low:
        out.append(dict(kind="RSI_LOW", text=f"RSI({C['rsi_period']}) = {r.rsi:.0f} — зона перепроданности",
                        confirmed=confirmed, plan=mean_rev_plan(entry_soft, low3 - 0.5 * atr)))
    # 3) возврат RSI выше порога
    if p.rsi < t_low <= r.rsi:
        out.append(dict(kind="RSI_REBOUND", text=f"RSI вернулся выше {t_low}: {p.rsi:.0f} → {r.rsi:.0f}",
                        confirmed=True, plan=mean_rev_plan(r.close, low5 - 0.5 * atr)))
    # 4) пробой максимума на объёме
    if r.close > r.brk_level and p.close <= r.brk_level and r.vol_ratio >= C["breakout_vol_mult"]:
        measured = r.brk_level - r.low_n
        plan = _plan(r.close, r.brk_level - atr, r.close + 0.5 * measured, r.close + measured) if measured > 0 else None
        out.append(dict(kind="BREAKOUT",
                        text=f"Пробой {C['breakout_window']}-дневного максимума ({px(r.brk_level)}) при объёме ×{r.vol_ratio:.1f}",
                        confirmed=True, plan=plan))
    # 5) пересечение SMA снизу вверх
    if p.sma_fast <= p.sma_slow and r.sma_fast > r.sma_slow:
        plan = _plan(r.close, r.sma_slow - 0.5 * atr, r.close + 3 * atr, r.close + 5 * atr)
        out.append(dict(kind="SMA_CROSS", text=f"SMA{C['sma_fast']} пересекла SMA{C['sma_slow']} снизу вверх",
                        confirmed=True, plan=plan))
    return out


def pick_best(sigs):
    ok = [s for s in sigs if s["plan"] and s["plan"]["rr1"] >= C["min_rr"]]
    return max(ok, key=lambda s: s["plan"]["rr1"]) if ok else None


def analyze(df, secid):
    """Возвращает (датафрейм с индикаторами, все сигналы, лучший сигнал)."""
    d = add_indicators(df)
    sigs = signals_at(d, len(d) - 1, None)
    if any(s["kind"] == "DROP_VOL" for s in sigs):  # дивиденды грузим только при необходимости
        try:
            sigs = signals_at(d, len(d) - 1, fetch_dividends(secid))
        except Exception as e:
            log.warning("дивиденды %s не загрузились: %s", secid, e)
    return d, sigs, pick_best(sigs)


# ============================== СООБЩЕНИЯ ==============================
def candle_partial(d):
    now = dt.datetime.now(MSK)
    return d.index[-1].date() == now.date() and now.weekday() < 5 and now.time() < dt.time(18, 50)


def fmt_alert(secid, d, sigs, best):
    r, p, plan = d.iloc[-1], d.iloc[-2], best["plan"]
    day = (r.close / p.close - 1) * 100
    name = NAMES.get(secid, "")
    L = [f"📊 <b>{secid}</b>" + (f" — {html.escape(name)}" if name else ""),
         f"Цена: <b>{px(r.close)} ₽</b> ({day:+.1f}% за день), свеча {d.index[-1]:%d.%m.%Y}"]
    if candle_partial(d):
        L.append("⚠ Дневная свеча ещё не закрыта — сигнал может измениться.")
    L.append("")
    L.append("<b>Сигналы:</b>" + (" (совпало несколько — сигнал сильнее)" if len(sigs) > 1 else ""))
    for s in sigs:
        L.append("• " + html.escape(s["text"]))
    L.append(f"• Оборот: в среднем {r.turnover_avg / 1e6:.0f} млн ₽/день")
    L.append("")
    L.append("<b>Сценарий покупки</b> (не рекомендация):")
    if best["confirmed"]:
        L.append(f"Вход: ≈ {px(plan['entry'])} ₽ (свеча закрылась в плюс — разворот подтверждён)")
    else:
        L.append(f"Вход: только при пробое {px(plan['entry'])} ₽ (максимум сигнального дня). "
                 f"Разворот пока не подтверждён; если пробоя нет в ближайшие 1–3 дня — сигнал отменяется")
    L.append(f"Стоп: {px(plan['stop'])} ₽ (−{plan['risk_pct']:.1f}%)")
    L.append(f"Цель 1: {px(plan['t1'])} ₽ (+{(plan['t1'] / plan['entry'] - 1) * 100:.1f}%), прибыль/риск {plan['rr1']:.1f}")
    L.append(f"Цель 2: {px(plan['t2'])} ₽ (+{(plan['t2'] / plan['entry'] - 1) * 100:.1f}%), прибыль/риск {plan['rr2']:.1f}")
    L.append("Идея: часть позиции зафиксировать у цели 1, стоп подтянуть к цене входа.")
    if CAPITAL > 0:
        per_share = plan["entry"] - plan["stop"]
        n = math.floor(CAPITAL * RISK_PCT / 100 / per_share)
        n = min(n, math.floor(CAPITAL * MAX_POS_PCT / 100 / plan["entry"]))
        if n > 0:
            L.append(f"Ориентир объёма при риске {RISK_PCT:g}% капитала: до {n} акций (≈ {n * plan['entry']:,.0f} ₽); "
                     f"учтите размер лота.".replace(",", " "))
    L.append("")
    L.append("⚠ Автоматический скан по правилам. Перед входом проверьте новости (отчётность, санкции, дивиденды), "
             "причина падения может быть фундаментальной. Не инвестиционная рекомендация.")
    return "\n".join(L)


def fmt_check(secid, d, sigs, best):
    r = d.iloc[-1]
    L = [f"🔎 <b>{secid}</b> {html.escape(NAMES.get(secid, ''))}",
         f"Цена {px(r.close)} ₽, свеча {d.index[-1]:%d.%m.%Y}",
         f"Просадка от макс. за {C['drop_lookback']} дн.: {r.drop_pct:.1f}%",
         f"Объём к среднему: ×{r.vol_ratio:.1f}",
         f"RSI({C['rsi_period']}): {r.rsi:.0f}",
         f"SMA{C['sma_fast']} {'выше' if r.sma_fast > r.sma_slow else 'ниже'} SMA{C['sma_slow']}",
         f"ATR: {px(r.atr)} ₽ ({r.atr / r.close * 100:.1f}% от цены)",
         f"Средний оборот: {r.turnover_avg / 1e6:.0f} млн ₽/день"
         + (" — ниже порога ликвидности, сигналы отключены" if r.turnover_avg < C["min_turnover_rub"] else "")]
    if not sigs:
        L.append("\nСигналов сейчас нет.")
    else:
        L.append("\nСработали правила:")
        for s in sigs:
            rr = f", прибыль/риск {s['plan']['rr1']:.1f}" if s["plan"] else ", план не построен"
            L.append("• " + html.escape(s["text"]) + rr)
        if not best:
            L.append(f"Но ни один план не проходит порог прибыль/риск ≥ {C['min_rr']} — алерт не отправляется.")
    return "\n".join(L)


# ============================== TELEGRAM ==============================
def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=40)
    return r.json()


def send(text, chat_id=None):
    chat_id = chat_id or CHAT_ID
    if not TOKEN or not chat_id:  # режим без Telegram: печатаем в консоль
        print(html.unescape(re.sub(r"<[^>]+>", "", text)))
        print("-" * 50)
        return
    for i in range(0, len(text), 4000):
        res = tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000], parse_mode="HTML",
                 disable_web_page_preview=True)
        if not res.get("ok"):
            log.warning("Telegram отклонил сообщение: %s", res)


# ============================== СОСТОЯНИЕ ==============================
def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
    st.setdefault("watchlist", list(DEFAULT_WATCHLIST))
    st.setdefault("last_alert", {})
    st.setdefault("last_scan", "")
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ============================== СКАН ==============================
def scan(state, manual=False, only=None):
    wl = only or state["watchlist"]
    today = dt.datetime.now(MSK).date()
    checked = stale = 0
    hits, errors = [], []
    for secid in wl:
        try:
            df = fetch_candles(secid, C["history_days"])
            time.sleep(0.15)
            if len(df) < 70:
                errors.append(f"{secid}: мало данных")
                continue
            if not manual and df.index[-1].date() != today:
                stale += 1
                continue
            checked += 1
            d, sigs, best = analyze(df, secid)
            if not best:
                continue
            last = state["last_alert"].get(secid)
            if not manual and last and (today - dt.date.fromisoformat(last)).days < C["cooldown_days"]:
                continue
            send(fmt_alert(secid, d, sigs, best))
            state["last_alert"][secid] = today.isoformat()
            hits.append(secid)
        except Exception as e:
            log.warning("%s: %s", secid, e)
            errors.append(f"{secid}: {e}")
    save_state(state)
    if manual or C["daily_summary"] or hits:
        if checked == 0 and stale:
            msg = "Сегодня торгов не было (или свеча за сегодня ещё не появилась) — скан пропущен."
        else:
            msg = f"✅ Скан завершён: проверено {checked}, сигналов {len(hits)}" + (f" ({', '.join(hits)})" if hits else ".")
        if errors:
            msg += f"\nПропущено из-за ошибок: {len(errors)} (" + "; ".join(errors[:4]) + ")"
        send(msg)
    return hits


# ============================== БЭКТЕСТ ==============================
def simulate(d, i, sig):
    """Проверка одного сигнала на истории. Возвращает (pnl_%, R, исход, индекс выхода) или None."""
    plan, n = sig["plan"], len(d)
    stop, t1 = plan["stop"], plan["t1"]
    if sig["confirmed"]:
        entry_px, start = plan["entry"], i + 1
    else:
        entry_px, start = None, None
        for j in range(i + 1, min(i + 4, n)):
            if d["high"].iloc[j] >= plan["entry"]:
                entry_px, start = max(plan["entry"], d["open"].iloc[j]), j
                break
        if entry_px is None:
            return None
    if entry_px <= stop:
        return None
    end = start + C["hold_days"]
    if end > n:
        return None  # сделка ещё не завершилась
    for k in range(start, end):
        op, lo, hi = d["open"].iloc[k], d["low"].iloc[k], d["high"].iloc[k]
        if op <= stop:
            exit_px, why = op, "STOP"
        elif lo <= stop:
            exit_px, why = stop, "STOP"
        elif op >= t1:
            exit_px, why = op, "T1"
        elif hi >= t1:
            exit_px, why = t1, "T1"
        else:
            continue
        break
    else:
        k, exit_px, why = end - 1, d["close"].iloc[end - 1], "TIME"
    pnl = (exit_px / entry_px - 1) * 100 - C["cost_pct"]
    r_mult = (exit_px - entry_px) / (entry_px - stop)
    return pnl, r_mult, why, k


def backtest(tickers):
    rows = []
    for secid in tickers:
        try:
            df = fetch_candles(secid, C["backtest_days"])
            if len(df) < 150:
                print(f"{secid}: мало данных, пропуск")
                continue
            try:
                divs = fetch_dividends(secid)
            except Exception:
                divs = []
            d = add_indicators(df)
            free_at, last_sig = 0, -999
            for i in range(60, len(d) - 1):
                if i < free_at or i - last_sig < C["cooldown_days"]:
                    continue
                best = pick_best(signals_at(d, i, divs))
                if not best:
                    continue
                res = simulate(d, i, best)
                if not res:
                    continue
                pnl, r_mult, why, k = res
                rows.append(dict(ticker=secid, kind=best["kind"], pnl=pnl, R=r_mult, exit=why))
                free_at, last_sig = k + 1, i
            time.sleep(0.15)
        except Exception as e:
            print(f"{secid}: ошибка {e}")
    if not rows:
        print("Сделок по правилам не найдено.")
        return None
    t = pd.DataFrame(rows)

    def agg(g):
        return pd.Series({
            "сделок": len(g),
            "в плюсе, %": (g.pnl > 0).mean() * 100,
            "ср. результат, %": g.pnl.mean(),
            "ср. R": g.R.mean(),
            "дошли до цели 1, %": (g.exit == "T1").mean() * 100,
            "выбиты по стопу, %": (g.exit == "STOP").mean() * 100,
            "по времени, %": (g.exit == "TIME").mean() * 100,
        })
    by = t.groupby("kind").apply(agg, include_groups=False)
    by.loc["ВСЕ"] = agg(t)
    pd.set_option("display.width", 200)
    print("\nРезультаты бэктеста (комиссии и проскальзывание учтены; R — результат в единицах риска):\n")
    print(by.round(1).to_string())
    print("\nВажно: прошлые результаты не гарантируют будущих; выборка по отдельному сигналу может быть мала;"
          " сигналы по разным бумагам коррелируют (рыночные обвалы дают сразу много сделок).")
    return by


# ============================== КОМАНДЫ БОТА ==============================
HELP = ("Команды:\n"
        "/scan — проверить весь список сейчас\n"
        "/check SBER — метрики и сигналы по одной бумаге\n"
        "/list — список отслеживаемых бумаг\n"
        "/add TICKER — добавить бумагу (тикер на TQBR)\n"
        "/remove TICKER — убрать бумагу\n"
        "/help — эта справка\n\n"
        f"Автоматический скан по будням в {C['scan_hour']:02d}:{C['scan_minute']:02d} МСК.")


def handle(msg, state):
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    if not CHAT_ID:
        if cmd == "/start":
            send(f"Ваш chat id: <code>{chat}</code>\nВпишите его в файл .env как CHAT_ID и перезапустите бота.", chat)
        return
    if chat != CHAT_ID:
        return  # чужие чаты игнорируем
    if cmd in ("/start", "/help"):
        send(HELP, chat)
    elif cmd == "/list":
        send("Отслеживаю: " + ", ".join(state["watchlist"]), chat)
    elif cmd == "/scan":
        send("Запускаю скан, это займёт около минуты…", chat)
        scan(state, manual=True)
    elif cmd in ("/check", "/add", "/remove") and not args:
        send(f"После команды нужно указать тикер, например: <code>{cmd} SBER</code>\n"
             "(если нажать на команду в справке, Telegram отправит её без тикера — допишите его вручную)", chat)
    elif cmd == "/check" and args:
        secid = args[0].upper()
        try:
            df = fetch_candles(secid, C["history_days"])
            if len(df) < 70:
                send("Недостаточно данных по этому тикеру.", chat)
                return
            d, sigs, best = analyze(df, secid)
            send(fmt_check(secid, d, sigs, best), chat)
            if best:
                send(fmt_alert(secid, d, sigs, best), chat)
        except Exception as e:
            send(f"Ошибка: {html.escape(str(e))}", chat)
    elif cmd == "/add" and args:
        secid = args[0].upper()
        try:
            if secid in state["watchlist"]:
                send("Уже в списке.", chat)
            elif fetch_candles(secid, 30).empty:
                send("Тикер не найден на основной доске TQBR.", chat)
            else:
                state["watchlist"].append(secid)
                save_state(state)
                send(f"Добавил {secid}.", chat)
        except Exception as e:
            send(f"Ошибка: {html.escape(str(e))}", chat)
    elif cmd == "/remove" and args:
        secid = args[0].upper()
        if secid in state["watchlist"]:
            state["watchlist"].remove(secid)
            save_state(state)
            send(f"Убрал {secid}.", chat)
        else:
            send("Такого тикера нет в списке.", chat)
    else:
        send(HELP, chat)


def maybe_scheduled_scan(state):
    now = dt.datetime.now(MSK)
    due = now.weekday() < 5 and now.time() >= dt.time(C["scan_hour"], C["scan_minute"])
    if due and state.get("last_scan") != now.date().isoformat():
        state["last_scan"] = now.date().isoformat()
        save_state(state)
        scan(state, manual=False)


def run():
    if not TOKEN:
        sys.exit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example).")
    state = load_state()
    if CHAT_ID:
        send(f"🤖 Бот запущен. Слежу за {len(state['watchlist'])} бумагами, скан по будням в "
             f"{C['scan_hour']:02d}:{C['scan_minute']:02d} МСК. /help — команды.")
    else:
        log.warning("CHAT_ID не задан: напишите боту /start, он пришлёт ваш chat id.")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            res = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", params=params, timeout=45).json()
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    handle(u["message"], state)
        except Exception as e:
            log.warning("ошибка цикла: %s", e)
            time.sleep(5)
        if CHAT_ID:
            try:
                maybe_scheduled_scan(state)
            except Exception as e:
                log.warning("ошибка скана: %s", e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if cmd == "run":
        run()
    elif cmd == "scan":
        scan(load_state(), manual="--force" in sys.argv)
    elif cmd == "check" and args:
        secid = args[0].upper()
        d, sigs, best = analyze(fetch_candles(secid, C["history_days"]), secid)
        send(fmt_check(secid, d, sigs, best))
        if best:
            send(fmt_alert(secid, d, sigs, best))
    elif cmd == "backtest":
        backtest([a.upper() for a in args] or load_state()["watchlist"])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
