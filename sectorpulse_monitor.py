#!/usr/bin/env python3
"""
SectorPulse Daily Monitor — Email-to-SMS version (completely free)
Runs momentum analysis and texts you via your carrier's email gateway.
"""

import os, json, time, datetime, smtplib, requests
from email.mime.text import MIMEText

# ── Config (set as GitHub Secrets) ────────────────────────────────
FINNHUB_KEY  = os.environ['FINNHUB_KEY']
GMAIL_USER   = os.environ['GMAIL_USER']       # yourname@gmail.com
GMAIL_PASS   = os.environ['GMAIL_APP_PASS']   # Gmail App Password (16 chars)
# Your carrier SMS gateway — pick yours:
#   AT&T:     10digitnumber@txt.att.net
#   T-Mobile: 10digitnumber@tmomail.net
#   Verizon:  10digitnumber@vtext.com
#   Sprint:   10digitnumber@messaging.sprintpcs.com
SMS_GATEWAY  = os.environ['SMS_GATEWAY']      # e.g. 7135551234@tmomail.net
STATE_FILE   = 'sector_state.json'

BASE = 'https://finnhub.io/api/v1'

SECTORS = [
    ('XLK','Technology'), ('XLF','Financials'), ('XLE','Energy'),
    ('XLV','Health Care'), ('XLI','Industrials'), ('XLY','Cons. Discretionary'),
    ('XLP','Cons. Staples'), ('XLRE','Real Estate'), ('XLB','Materials'),
    ('XLU','Utilities'), ('XLC','Comm. Services'),
]

TOP_PICKS = {
    'XLK':'NVDA','XLF':'JPM','XLE':'XOM','XLV':'LLY','XLI':'GE',
    'XLY':'AMZN','XLP':'COST','XLRE':'PLD','XLB':'LIN','XLU':'NEE','XLC':'META'
}

# ── Finnhub fetch ─────────────────────────────────────────────────
def get(path, params={}):
    params['token'] = FINNHUB_KEY
    r = requests.get(f'{BASE}{path}', params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def fetch_sector(ticker):
    time.sleep(0.4)
    try:
        q = get('/quote', {'symbol': ticker})
        time.sleep(0.4)
        m = get('/stock/metric', {'symbol': ticker, 'metric': 'all'})
        met = m.get('metric', {})
        return {
            'ticker': ticker,
            'price':  q.get('c'),
            'ret5':   q.get('dp'),
            'rs4w':   met.get('priceRelativeToS&P5004Week'),
            'rs13w':  met.get('priceRelativeToS&P50013Week'),
            'rs26w':  met.get('priceRelativeToS&P50026Week'),
            'ret13':  met.get('13WeekPriceReturnDaily'),
            'ret26':  met.get('26WeekPriceReturnDaily'),
            'retYTD': met.get('yearToDatePriceReturnDaily'),
            'hi52':   met.get('52WeekHigh'),
            'lo52':   met.get('52WeekLow'),
            'vol10':  met.get('10DayAverageTradingVolume'),
            'vol3m':  met.get('3MonthAverageTradingVolume'),
        }
    except Exception as e:
        print(f'  Error {ticker}: {e}')
        return None

# ── Momentum engine ───────────────────────────────────────────────
def compute(d):
    score = 0.0
    rs13 = d.get('rs13w')
    rs4  = d.get('rs4w')

    if rs13 is not None:
        if rs13>5: score+=6
        elif rs13>1: score+=3
        elif rs13>-1: score+=0
        elif rs13>-5: score-=3
        else: score-=6

    mom = None
    if rs4 is not None and rs13 is not None:
        mom = rs4 - rs13
        d['rsMom'] = mom
        if mom>3: score+=6
        elif mom>0.5: score+=3
        elif mom>-0.5: score+=0
        elif mom>-3: score-=3
        else: score-=6
    else:
        d['rsMom'] = None

    rets = [d.get('ret5'), d.get('ret13'), d.get('ret26'), d.get('retYTD')]
    pos = sum(1 for r in rets if r is not None and r > 0)
    neg = sum(1 for r in rets if r is not None and r < 0)
    d['tfAlign'] = pos - neg
    if pos>=3: score+=4
    elif pos==2: score+=2
    elif neg==2: score-=2
    elif neg>=3: score-=4

    hi, lo, price = d.get('hi52'), d.get('lo52'), d.get('price')
    if hi and lo and price and hi != lo:
        w = (price-lo)/(hi-lo)*100
        d['w52pos'] = w
        if w>80: score+=3
        elif w>60: score+=1.5
        elif w<40: score-=1.5
        elif w<20: score-=3

    v10, v3m = d.get('vol10'), d.get('vol3m')
    if v10 and v3m and v3m > 0:
        vt = (v10/v3m - 1)*100
        d['volTrend'] = vt
        if vt>15: score+=1
        elif vt>0: score+=0.5
        elif vt<-15: score-=1
        elif vt<0: score-=0.5
    else:
        d['volTrend'] = None

    r5, r13 = d.get('ret5'), d.get('ret13')
    if r5 is not None and r13 is not None:
        score += 0.5 if (r5>0)==(r13>0) else -0.5

    max_s = 6+6+4+3+1+0.5
    d['score'] = round(max(0, min(10, (score+max_s)/(2*max_s)*10)), 2)

    mom2 = d.get('rsMom', 0) or 0
    if rs13 is not None:
        if rs13>=0 and mom2>=0:   d['quadrant']='leading'
        elif rs13<0 and mom2>=0:  d['quadrant']='improving'
        elif rs13>=0 and mom2<0:  d['quadrant']='weakening'
        else:                      d['quadrant']='lagging'
    else:
        d['quadrant']='unknown'
    return d

# ── Anomaly detection ─────────────────────────────────────────────
def detect(current, previous):
    alerts = []
    for ticker, name in SECTORS:
        cur  = current.get(ticker)
        prev = previous.get(ticker)
        if not cur: continue

        # Strong buy
        if cur['score'] >= 8.5:
            alerts.append(f"🚀 {ticker} score {cur['score']}/10 ({cur['quadrant'].upper()})")

        if not prev: continue

        # Quadrant flip
        if cur['quadrant'] != prev.get('quadrant',''):
            q_rank = {'leading':0,'improving':1,'weakening':2,'lagging':3}
            qc = q_rank.get(cur['quadrant'],2)
            qp = q_rank.get(prev.get('quadrant',''),2)
            e = '📈' if qc < qp else '📉'
            alerts.append(f"{e} {ticker}: {prev.get('quadrant','?').upper()} → {cur['quadrant'].upper()}")

        # Score jump
        delta = cur['score'] - prev.get('score', cur['score'])
        if abs(delta) >= 1.5:
            e = '⬆️' if delta > 0 else '⬇️'
            alerts.append(f"{e} {ticker} score {delta:+.1f} → {cur['score']}/10")

        # Volume spike
        vt = cur.get('volTrend') or 0
        vp = prev.get('volTrend') or 0
        if vt > 40 and vp < 20:
            alerts.append(f"🔊 {ticker} vol +{vt:.0f}% above avg")

    return alerts

# ── Build SMS (kept short for text messages) ──────────────────────
def build_sms(current, alerts, top_ticker, top_stock):
    today = datetime.date.today().strftime('%m/%d')
    top   = current[top_ticker]
    lines = [
        f"📈 SectorPulse {today}",
        f"Top: {top_ticker} {top['score']}/10 → buy {top_stock}",
        f"RS:{top.get('rs13w',0):+.1f}% Mom:{top.get('rsMom',0):+.1f}",
    ]
    if alerts:
        lines.append(f"\n⚡ {len(alerts)} alert(s):")
        for a in alerts[:4]:   # cap at 4 so SMS stays short
            lines.append(f"  {a}")
    else:
        lines.append("✅ No anomalies.")

    # Top 3 sectors AFTER the top pick (avoids repeating it)
    all_sorted = sorted(current.values(), key=lambda d: d['score'], reverse=True)
    next3 = [d for d in all_sorted if d['ticker'] != top_ticker][:3]
    lines.append(f"\nAlso watching:")
    for d in next3:
        lines.append(f"  {d['ticker']} {d['score']} {d['quadrant'][:4].upper()}")
    lines.append("sectorpulse.app")  # replace with your real URL
    return '\n'.join(lines)

# ── Send via Gmail SMTP ───────────────────────────────────────────
def send(to, subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From']    = GMAIL_USER
    msg['To']      = to
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, to, msg.as_string())
    print(f'  ✅ Sent to {to}')

# ── State ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {}

def save_state(d):
    with open(STATE_FILE, 'w') as f: json.dump(d, f, indent=2)

# ── Main ──────────────────────────────────────────────────────────
def main():
    print(f'SectorPulse {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}')
    previous = load_state()

    current = {}
    for ticker, name in SECTORS:
        print(f'  {ticker}...', end='', flush=True)
        d = fetch_sector(ticker)
        if d:
            d['name'] = name
            d = compute(d)
            current[ticker] = d
            print(f' {d["score"]}')

    if not current:
        print('No data — aborting')
        return

    alerts = detect(current, previous)
    best   = max(current.values(), key=lambda d: d['score'])
    stock  = TOP_PICKS.get(best['ticker'], '—')

    print(f'Top: {best["ticker"]} ({best["score"]}) → {stock}')
    print(f'Alerts: {len(alerts)}')

    sms = build_sms(current, alerts, best['ticker'], stock)
    print('\n--- SMS Preview ---')
    print(sms)
    print('-------------------')

    send(SMS_GATEWAY, '', sms)   # SMS gateways ignore subject line
    save_state(current)
    print('Done ✅')

if __name__ == '__main__':
    main()
