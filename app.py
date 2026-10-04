"""
EURUSD Engine v2
Fondamental -> Biais -> Niveaux (liquidité, VWAP, POC) -> Réaction/absorption -> DXY -> SETUP CONFIRMED -> Entry/SL/TP/R:R

Outil d'aide à la décision, pas un conseil financier. À tester en démo avant tout argent réel.

Variables d'environnement (Render) :
  FRED_KEY : obligatoire (gratuit) https://fred.stlouisfed.org/docs/api/api_key.html
  TD_KEY   : optionnel (gratuit) https://twelvedata.com  (sinon Yahoo Finance est utilisé)
"""
import os
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, Response

FRED_KEY = os.getenv("FRED_KEY")
TD_KEY = os.getenv("TD_KEY")
PIP = 0.0001
MIN_RR = 1.5      # R:R minimum pour valider un setup
BIAS_TH = 2       # score fondamental minimum pour avoir un biais
MIN_CHECKS = 3    # confirmations de réaction requises (sur 4)
UA = {"User-Agent": "Mozilla/5.0"}

app = Flask(__name__)
LOCK = threading.Lock()
CACHE = {}
STATE = {"plan": None, "log": []}

SUPPORT = {"EQL", "Low", "PDL", "VWAP", "POC"}   # niveaux d'entrée côté achat
RESIST = {"EQH", "High", "PDH", "VWAP", "POC"}   # niveaux d'entrée côté vente


def cached(key, ttl, fn):
    e = CACHE.get(key)
    if e and time.time() - e[0] < ttl:
        return e[1]
    try:
        v = fn()
    except Exception:
        if e:
            return e[1]  # garde l'ancienne valeur si la source tombe
        raise
    CACHE[key] = (time.time(), v)
    return v


def log(msg):
    STATE["log"].insert(0, {"t": datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC"), "msg": msg})
    del STATE["log"][12:]


# ====================== DONNÉES ======================
def fred(series, n=15):
    if not FRED_KEY:
        raise RuntimeError("FRED_KEY manquante")
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params=dict(series_id=series, api_key=FRED_KEY, file_type="json",
                    sort_order="desc", limit=n), timeout=20).json()
    return [float(o["value"]) for o in r["observations"] if o["value"] != "."]


def yahoo(symbol, rng, interval):
    r = requests.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
        params=dict(range=rng, interval=interval), headers=UA, timeout=20).json()
    res = r["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i in range(len(res["timestamp"])):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        out.append(dict(t=res["timestamp"][i], o=q["open"][i], h=q["high"][i],
                        l=q["low"][i], c=q["close"][i], v=q["volume"][i] or 0))
    return out


def twelve(symbol, interval, n):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params=dict(symbol=symbol, interval=interval, outputsize=n, apikey=TD_KEY),
        timeout=20).json()
    if "values" not in r:
        raise RuntimeError(r.get("message", "Twelve Data error"))
    return [dict(t=0, o=float(x["open"]), h=float(x["high"]), l=float(x["low"]),
                 c=float(x["close"]), v=0) for x in r["values"][::-1]]


def eurusd():
    if TD_KEY:
        try:
            return twelve("EUR/USD", "1h", 300), "Twelve Data"
        except Exception:
            pass
    return yahoo("EURUSD=X", "1mo", "1h"), "Yahoo Finance"


def futures_bars():
    try:
        return [x for x in yahoo("6E=F", "5d", "15m") if x["v"] > 0]
    except Exception:
        return []


# ====================== 1. FONDAMENTAL ======================
def fundamentals():
    items = []

    def add(label, score):
        items.append({"label": label, "score": score})

    diff = fred("DFF", 1)[0] - fred("ECBDFR", 1)[0]
    add("Écart de taux Fed-BCE : %.2f pt" % diff, -1 if diff > 1 else (1 if diff < 0.5 else 0))

    y2 = fred("DGS2", 25)
    ch2 = y2[0] - y2[-1]
    add("Rendement US 2 ans (1 mois) : %+.2f pt" % ch2, -1 if ch2 > 0.15 else (1 if ch2 < -0.15 else 0))

    y10 = fred("DGS10", 25)
    ch10 = y10[0] - y10[-1]
    add("Rendement US 10 ans (1 mois) : %+.2f pt" % ch10, -1 if ch10 > 0.15 else (1 if ch10 < -0.15 else 0))

    cpi = fred("CPIAUCSL", 15)
    now_, prev_ = cpi[0] / cpi[12] - 1, cpi[1] / cpi[13] - 1
    d = (now_ - prev_) * 100
    add("Inflation US : %.2f%% (tendance %+.2f)" % (now_ * 100, d), -1 if d > 0.1 else (1 if d < -0.1 else 0))

    u = fred("UNRATE", 4)
    du = u[0] - u[-1]
    add("Chômage US : %.1f%% (%+.1f)" % (u[0], du), 1 if du > 0.1 else (-1 if du < -0.1 else 0))

    try:
        dx = yahoo("DX-Y.NYB", "3mo", "1d")
        avg = sum(x["c"] for x in dx[-20:]) / 20
        dev = dx[-1]["c"] / avg - 1
        add("DXY %.2f vs moyenne 20j : %+.2f%%" % (dx[-1]["c"], dev * 100),
            -1 if dev > 0.005 else (1 if dev < -0.005 else 0))
    except Exception:
        add("DXY quotidien indisponible", 0)

    try:
        g = fred("A191RL1Q225SBEA", 1)[0]
        add("PIB US annualisé : %.1f%% (contexte)" % g, 0)
    except Exception:
        pass
    return items


# ====================== 2. ORDER FLOW (proxys gratuits) ======================
def close_delta(bars):
    """Delta estimé : volume pondéré par la position de la clôture dans la bougie."""
    tot = sum(x["v"] for x in bars) or 1
    d = sum(x["v"] * ((2 * x["c"] - x["h"] - x["l"]) / (x["h"] - x["l"]) if x["h"] > x["l"] else 0)
            for x in bars)
    return d / tot


def orderflow():
    items = []
    try:  # COT CFTC : spéculateurs sur l'Euro FX (hebdomadaire)
        rows = requests.get(
            "https://publicreporting.cftc.gov/resource/6dca-aqww.json",
            params={"cftc_contract_market_code": "099741",
                    "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 6},
            headers=UA, timeout=20).json()
        net = [int(float(r["noncomm_positions_long_all"])) - int(float(r["noncomm_positions_short_all"]))
               for r in rows]
        ch = net[0] - net[3]
        items.append({"label": "COT spéculateurs euro : net %+d (4 sem. : %+d)" % (net[0], ch),
                      "score": 1 if ch > 5000 else (-1 if ch < -5000 else 0)})
    except Exception:
        items.append({"label": "COT indisponible", "score": 0})
    try:  # delta estimé des futures Euro FX (CME), 24 dernières heures
        f = cached("fut", 240, futures_bars)[-96:]
        r = close_delta(f)
        items.append({"label": "Delta estimé futures 6E (24 h) : %+.1f%%" % (r * 100),
                      "score": 1 if r > 0.05 else (-1 if r < -0.05 else 0)})
    except Exception:
        items.append({"label": "Delta futures 6E indisponible", "score": 0})
    return items


# ====================== 3. NEWS ======================
def calendar():
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=20).json()
    now = datetime.now(timezone.utc)
    out = []
    for e in r:
        if e.get("impact") != "High" or e.get("country") not in ("USD", "EUR"):
            continue
        t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        m = int((t - now).total_seconds() / 60)
        if -30 <= m <= 24 * 60:
            out.append({"title": e["title"], "country": e["country"],
                        "time": t.strftime("%a %H:%M UTC"), "minutes": m})
    return sorted(out, key=lambda x: x["minutes"])


# ====================== 4. NIVEAUX : liquidité + VWAP + POC ======================
def atr(c, n=14):
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
           for i in range(len(c) - n, len(c))]
    return sum(trs) / n


def swings(c, k=3):
    hi, lo = [], []
    for i in range(k, len(c) - k):
        w = c[i - k:i + k + 1]
        if c[i]["h"] == max(x["h"] for x in w):
            hi.append((i, c[i]["h"]))
        if c[i]["l"] == min(x["l"] for x in w):
            lo.append((i, c[i]["l"]))
    return hi, lo


def is_equal(level, pool, tol):
    return sum(1 for _, p in pool if abs(p - level) <= tol) >= 2


def vwap_poc(f):
    """VWAP de la journée (UTC) et POC sur ~3 jours, à partir des futures 6E 15 min (volume réel CME)."""
    if len(f) < 20:
        return None, None
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day = [x for x in f if x["t"] >= midnight]
    if len(day) < 6:
        day = f[-48:]
    tp = lambda x: (x["h"] + x["l"] + x["c"]) / 3
    vwap = sum(tp(x) * x["v"] for x in day) / sum(x["v"] for x in day)
    prof = {}
    for x in f[-288:]:
        k = round(tp(x) / 0.0005)
        prof[k] = prof.get(k, 0) + x["v"]
    poc = max(prof, key=prof.get) * 0.0005
    return vwap, poc


def level_pool(c, vw, poc):
    """Liste de (prix, type) en espace spot."""
    a = atr(c)
    tol = 0.1 * a
    hi, lo = swings(c)
    pool = [(p, "EQH" if is_equal(p, hi, tol) else "High") for _, p in hi]
    pool += [(p, "EQL" if is_equal(p, lo, tol) else "Low") for _, p in lo]
    if len(c) >= 48:
        pool.append((max(x["h"] for x in c[-48:-24]), "PDH"))
        pool.append((min(x["l"] for x in c[-48:-24]), "PDL"))
    if vw:
        pool.append((vw, "VWAP"))
    if poc:
        pool.append((poc, "POC"))
    return pool


def nearby(pool, price):
    seen, up, dn = set(), [], []
    for p, k in sorted(pool):
        key = round(p, 4)
        if key in seen:
            continue
        seen.add(key)
        (up if p > price else dn).append({"p": round(p, 5), "k": k})
    return {"above": up[:4], "below": dn[-4:][::-1]}


# ====================== 5. RÉACTION / ABSORPTION ======================
def reaction(bars, level, a):
    """Espace 'achat' : on cherche une réaction haussière sur un niveau support.
    bars = bougies 15 min des futures (déjà converties en prix spot)."""
    if len(bars) < 24:
        return False, []
    last, base = bars[-4:], bars[-24:-4]
    avgv = sum(x["v"] for x in base) / len(base)
    tol = 0.35 * a
    touched = any(x["l"] <= level + tol for x in last)
    rej = absorb = False
    for x in last:
        rng = x["h"] - x["l"]
        if rng <= 0 or x["l"] > level + tol:
            continue
        lower_wick = (min(x["o"], x["c"]) - x["l"]) / rng
        body = abs(x["c"] - x["o"]) / rng
        if lower_wick >= 0.5 and x["c"] >= x["l"] + 0.5 * rng:
            rej = True
        if avgv > 0 and x["v"] >= 1.5 * avgv and body <= 0.4:
            absorb = True
    checks = [
        {"label": "Rejet : longue mèche + clôture haute", "ok": rej},
        {"label": "Absorption : gros volume, petit corps", "ok": absorb},
        {"label": "Delta acheteur (proxy)", "ok": close_delta(last) > 0.1},
        {"label": "Niveau repris par le prix", "ok": last[-1]["c"] > level},
    ]
    return touched, checks


# ====================== 6. DXY ======================
def dxy_gate(d):
    """Le DXY doit aller dans le sens inverse de la position EURUSD."""
    try:
        x = cached("dxy15", 240, lambda: yahoo("DX-Y.NYB", "5d", "15m"))
        cl = [b["c"] for b in x]
        if len(cl) < 25:
            raise ValueError
    except Exception:
        return "NEUTRE", "DXY intraday indisponible", None
    ma = sum(cl[-20:]) / 20
    below, falling = cl[-1] < ma, cl[-1] < cl[-7]
    dxy_dir = -1 if (below and falling) else (1 if (not below and not falling) else 0)
    eu_dir = -dxy_dir
    state = "NEUTRE" if eu_dir == 0 else ("CONFIRME" if eu_dir == d else "CONTRE")
    txt = "DXY %.3f : %s sa moyenne 15m, %s sur ~1h30" % (
        cl[-1], "sous" if below else "au-dessus de", "en baisse" if falling else "en hausse")
    return state, txt, round(cl[-1], 3)


# ====================== 7. SETUP ======================
def make_plan(d, entry, sl, targets, mode, level, lk):
    risk = entry - sl
    if risk <= 0:
        return None
    pick = None
    for i, (t, k) in enumerate(targets):
        if t > entry and (t - entry) / risk >= MIN_RR:
            pick = i
            break
    if pick is None:
        return None
    tp, tk = targets[pick]
    tp2 = targets[pick + 1] if pick + 1 < len(targets) else None
    return {
        "dir": d, "mode": mode, "entry": d * entry, "sl": d * sl,
        "tp": d * tp, "tp_kind": tk, "rr": round((tp - entry) / risk, 2),
        "tp2": d * tp2[0] if tp2 else None, "tp2_kind": tp2[1] if tp2 else None,
        "rr2": round((tp2[0] - entry) / risk, 2) if tp2 else None,
        "level": d * level, "level_kind": lk,
        "pips_sl": round(risk / PIP), "pips_tp": round((tp - entry) / PIP), "t": time.time(),
    }


def build_setup(c, d, fbars, vw, poc):
    """d=+1 achat, d=-1 vente. Pour la vente, on inverse les prix puis on ré-inverse."""
    a = atr(c)
    price = c[-1]["c"]
    off = (price - fbars[-1]["c"]) if fbars else 0.0   # écart spot / futures
    pool = level_pool(c, vw + off if vw else None, poc + off if poc else None)

    P = d * price
    entry_set, tgt_set = (SUPPORT, RESIST) if d == 1 else (RESIST, SUPPORT)
    cands = [(d * p, k) for p, k in pool if k in entry_set and d * p < P]
    if not cands:
        return None
    level, lk = max(cands)
    targets = sorted((d * p, k) for p, k in pool if k in tgt_set and d * p > P)

    bars = []
    for x in fbars[-28:]:
        o, h, l, cl = x["o"] + off, x["h"] + off, x["l"] + off, x["c"] + off
        bars.append(dict(o=d * o, h=d * (h if d == 1 else l), l=d * (l if d == 1 else h), c=d * cl, v=x["v"]))
    touched, checks = reaction(bars, level, a)
    n_ok = sum(1 for k in checks if k["ok"])

    if touched and bars:
        sl = min(min(x["l"] for x in bars[-4:]), level) - 0.3 * a
        plan = make_plan(d, P, sl, targets, "IMMÉDIATE : réaction sur le niveau", level, lk)
    else:
        plan = make_plan(d, level, level - 0.5 * a, targets, "LIMITE (prévisionnel) : attendre le niveau", level, lk)
    if plan and plan["pips_sl"] > 2.5 * a / PIP:
        plan = None
    return {"level": d * level, "kind": lk, "touched": touched, "checks": checks,
            "n_ok": n_ok, "plan": plan, "dist_pips": round(abs(P - level) / PIP)}


# ====================== MOTEUR ======================
def sign(x, th):
    return 1 if x >= th else (-1 if x <= -th else 0)


def analyse():
    fund = cached("fund", 3600, fundamentals)
    flow = cached("flow", 1800, orderflow)
    try:
        news = cached("cal", 900, calendar)
    except Exception:
        news = []
    c, src = cached("px", 240, eurusd)
    fbars = cached("fut", 240, futures_bars)
    vw, poc = vwap_poc(fbars)
    price = c[-1]["c"]
    off = (price - fbars[-1]["c"]) if fbars else 0.0

    ftotal = sum(i["score"] for i in fund)
    fb = sign(ftotal, BIAS_TH)
    ofb = sign(sum(i["score"] for i in flow), 1)
    conf = "NEUTRE" if (ofb == 0 or fb == 0) else ("CONFIRME" if ofb == fb else "CONTRE")
    news_risk = any(-30 <= n["minutes"] <= 120 for n in news)
    dstate, dtxt, dval = dxy_gate(fb if fb else 1)

    plan, setup, status, msg = STATE["plan"], None, "NEUTRE", ""

    if plan:  # surveillance du scénario actif
        d = plan["dir"]
        hit_sl = price <= plan["sl"] if d == 1 else price >= plan["sl"]
        hit_tp = price >= plan["tp"] if d == 1 else price <= plan["tp"]
        dstate_p = dxy_gate(d)[0]
        reason = None
        if hit_tp:
            log("🎯 Objectif atteint (%.5f)" % price)
            STATE["plan"] = plan = None
            status, msg = "OBJECTIF ATTEINT", "TP touché : scénario terminé."
        elif fb != d:
            reason = "le biais fondamental a changé"
        elif hit_sl:
            reason = "niveau d'invalidation cassé (%.5f)" % price
        elif dstate_p == "CONTRE":
            reason = "le DXY est passé contre le scénario"
        elif conf == "CONTRE":
            reason = "l'order flow est passé contre le scénario"
        elif time.time() - plan["t"] > 12 * 3600:
            reason = "scénario expiré (12 h)"
        if reason:
            log("⛔ STOP : " + reason)
            STATE["plan"] = plan = None
            status, msg = "STOP", "Scénario arrêté : " + reason
        elif plan:
            status, msg = "EN COURS", "Le scénario suit toujours l'analyse."

    if not plan and status not in ("STOP", "OBJECTIF ATTEINT"):
        if news_risk:
            status, msg = "PAUSE NEWS", "News majeure USD/EUR proche : pas de nouvelle entrée."
        elif fb == 0:
            status, msg = "NEUTRE", "Biais fondamental neutre : pas de trade."
        elif conf == "CONTRE":
            status, msg = "ATTENTE", "Order flow contre le biais : on attend l'alignement."
        elif not fbars:
            status, msg = "ATTENTE", "Données futures 6E indisponibles (marché fermé ?)."
        else:
            setup = build_setup(c, fb, fbars, vw, poc)
            if not setup or not setup["plan"]:
                status, msg = "ATTENTE", "Biais présent mais aucun niveau avec R:R >= %.1f." % MIN_RR
            elif setup["touched"] and setup["n_ok"] >= MIN_CHECKS:
                if dstate == "CONFIRME":
                    STATE["plan"] = plan = setup["plan"]
                    status, msg = "SETUP CONFIRMED", "Fondamental + niveau + réaction + DXY alignés."
                    log("🟢 SETUP CONFIRMED %s @ %.5f (R:R %.2f)" % (
                        "ACHAT" if fb == 1 else "VENTE", plan["entry"], plan["rr"]))
                else:
                    status, msg = "RÉACTION", "Réaction validée, mais le DXY ne confirme pas (%s)." % dstate
            elif setup["touched"]:
                status, msg = "RÉACTION", "Niveau touché : %d/4 confirmations seulement." % setup["n_ok"]
            else:
                status = "SURVEILLANCE"
                msg = "Biais %s. Niveau surveillé à %d pips du prix." % (
                    "haussier" if fb == 1 else "baissier", setup["dist_pips"])

    return {"price": price, "source": src, "status": status, "message": msg,
            "bias": fb, "ftotal": ftotal, "fund": fund, "flow": flow, "conf": conf,
            "dxy": {"state": dstate, "text": dtxt}, "setup": setup,
            "plan": plan, "pool": nearby(level_pool(c, vw + off if vw else None, poc + off if poc else None), price),
            "vwap": round(vw + off, 5) if vw else None, "poc": round(poc + off, 5) if poc else None,
            "news": news[:5], "log": STATE["log"],
            "updated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}


@app.route("/api/analysis")
def api():
    try:
        with LOCK:
            return jsonify(analyse())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/health")
def health():
    return "ok"


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>EURUSD Engine</title>
<style>
:root{--bg:#0e1116;--card:#171c24;--tx:#e6e9ee;--mut:#8b94a3;--g:#22c55e;--r:#ef4444;--y:#eab308;--b:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px system-ui,sans-serif;padding:12px;max-width:640px;margin:auto}
.card{background:var(--card);border-radius:12px;padding:14px;margin-bottom:12px}
h2{margin:0 0 8px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px}.price{font-size:26px;font-weight:700}
.badge{padding:6px 12px;border-radius:20px;font-weight:700;font-size:13px;text-align:center}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #222a35;gap:8px}
.row:last-child{border:0}.s{font-weight:700;min-width:24px;text-align:right}
.mut{color:var(--mut);font-size:12px}button{background:var(--b);color:#fff;border:0;border-radius:8px;padding:8px 12px}
</style></head><body>
<div class="card"><div class="top"><div><div class="mut">EUR/USD</div><div class="price" id="price">...</div></div>
<div class="badge" id="status">Chargement</div></div>
<p id="msg" style="margin:10px 0 0"></p><div class="mut" id="upd"></div></div>
<div id="app"></div>
<div class="card"><button onclick="Notification.requestPermission()">Activer les notifications</button>
<div class="mut" style="margin-top:6px">Mise à jour toutes les 60 s tant que la page est ouverte. Outil d&#39;aide, pas un conseil financier.</div></div>
<script>
var last=null;
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function col(s){return s>0?'var(--g)':s<0?'var(--r)':'var(--mut)'}
function f5(x){return Number(x).toFixed(5)}
function rows(a){return a.map(function(i){return '<div class="row"><span>'+esc(i.label)+'</span><span class="s" style="color:'+col(i.score)+'">'+(i.score>0?'+':'')+i.score+'</span></div>'}).join('')}
function badgeColor(s){if(s=='SETUP CONFIRMED'||s=='EN COURS'||s=='OBJECTIF ATTEINT')return '--g';if(s=='STOP')return '--r';if(s=='SURVEILLANCE'||s=='RÉACTION')return '--b';if(s=='ATTENTE'||s=='PAUSE NEWS')return '--y';return '--mut'}
function render(d){
 if(d.error){document.getElementById('msg').textContent='Erreur : '+d.error;return}
 var st=document.getElementById('status');st.textContent=d.status;st.style.background='var('+badgeColor(d.status)+')';st.style.color='#000';
 document.getElementById('price').textContent=f5(d.price);
 document.getElementById('msg').textContent=d.message;
 document.getElementById('upd').textContent='Source '+d.source+' | MAJ '+d.updated;
 var b=d.bias>0?'HAUSSIER':d.bias<0?'BAISSIER':'NEUTRE';
 var h='<div class="card"><h2>1. Biais fondamental : '+b+' ('+(d.ftotal>0?'+':'')+d.ftotal+')</h2>'+rows(d.fund)+'</div>';
 h+='<div class="card"><h2>Order flow (proxys) : '+d.conf+'</h2>'+rows(d.flow)+'<div class="mut">Pas de carnet d&#39;ordres en forex gratuit : COT hebdo + delta estimé des futures CME 6E.</div></div>';
 if(d.setup){var s=d.setup;h+='<div class="card"><h2>2-3. Niveau surveillé : '+s.kind+' '+f5(s.level)+'</h2>'
 +'<div class="mut" style="margin-bottom:6px">'+(s.touched?'Niveau touché':'À '+s.dist_pips+' pips du prix')+' | '+s.n_ok+'/4 confirmations</div>'
 +s.checks.map(function(k){return '<div class="row"><span>'+esc(k.label)+'</span><span class="s" style="color:'+(k.ok?'var(--g)':'var(--mut)')+'">'+(k.ok?'OK':'-')+'</span></div>'}).join('')+'</div>'}
 var dc=d.dxy.state=='CONFIRME'?'var(--g)':d.dxy.state=='CONTRE'?'var(--r)':'var(--mut)';
 h+='<div class="card"><h2>4. DXY : <span style="color:'+dc+'">'+d.dxy.state+'</span></h2><div class="mut">'+esc(d.dxy.text)+'</div></div>';
 var p=d.plan||(d.setup&&d.setup.plan);
 if(p){h+='<div class="card"><h2>5. Plan '+(d.plan?'(actif)':'(prévisionnel)')+' : '+(p.dir>0?'ACHAT':'VENTE')+'</h2>'
 +'<div class="row"><span>Entrée</span><b>'+f5(p.entry)+'</b></div><div class="mut">'+esc(p.mode)+'</div>'
 +'<div class="row"><span>Invalidation (SL)</span><b style="color:var(--r)">'+f5(p.sl)+' (-'+p.pips_sl+' pips)</b></div>'
 +'<div class="row"><span>TP1 ('+p.tp_kind+')</span><b style="color:var(--g)">'+f5(p.tp)+' (+'+p.pips_tp+' pips)</b></div>'
 +'<div class="row"><span>R:R TP1</span><b>'+p.rr+'</b></div>'
 +(p.tp2?'<div class="row"><span>TP2 ('+p.tp2_kind+')</span><b>'+f5(p.tp2)+' (R:R '+p.rr2+')</b></div>':'')+'</div>'}
 h+='<div class="card"><h2>Niveaux clés</h2>'
 +(d.vwap?'<div class="row"><span>VWAP du jour</span><b>'+f5(d.vwap)+'</b></div>':'')
 +(d.poc?'<div class="row"><span>POC (3 jours)</span><b>'+f5(d.poc)+'</b></div>':'')
 +d.pool.above.slice().reverse().map(function(x){return '<div class="row"><span>'+x.k+' au-dessus</span><b>'+f5(x.p)+'</b></div>'}).join('')
 +d.pool.below.map(function(x){return '<div class="row"><span>'+x.k+' en dessous</span><b>'+f5(x.p)+'</b></div>'}).join('')+'</div>';
 h+='<div class="card"><h2>News majeures USD/EUR</h2>'+(d.news.length?d.news.map(function(n){return '<div class="row"><span>'+esc(n.country+' '+n.title)+'</span><span class="mut">'+esc(n.time)+'</span></div>'}).join(''):'<div class="mut">Aucune dans les 24 h</div>')+'</div>';
 h+='<div class="card"><h2>Journal</h2>'+(d.log.length?d.log.map(function(l){return '<div class="row"><span>'+esc(l.msg)+'</span><span class="mut">'+esc(l.t)+'</span></div>'}).join(''):'<div class="mut">Rien pour le moment</div>')+'</div>';
 document.getElementById('app').innerHTML=h;
 if(last&&last!==d.status&&window.Notification&&Notification.permission==='granted')new Notification('EURUSD : '+d.status,{body:d.message});
 last=d.status;
}
function load(){fetch('/api/analysis').then(function(r){return r.json()}).then(render).catch(function(){document.getElementById('msg').textContent='Connexion impossible'})}
load();setInterval(load,60000);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
