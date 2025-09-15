# app/services/indicators.py
from statistics import mean, pstdev

def ema(series, period):
    if not series or period <= 1 or len(series) < period:
        return [None]*len(series)
    k = 2/(period+1)
    out = [None]*(period-1)
    m = sum(series[:period])/period
    out.append(m)
    for i in range(period, len(series)):
        m = series[i]*k + out[-1]*(1-k)
        out.append(m)
    return out

def rsi(closes, period=14):
    if len(closes) < period+1:
        return [None]*len(closes)
    gains, losses = [], []
    for i in range(1, period+1):
        ch = closes[i]-closes[i-1]
        gains.append(max(ch,0))
        losses.append(abs(min(ch,0)))
    avg_gain, avg_loss = mean(gains), mean(losses)
    out = [None]*period
    rs = (avg_gain/avg_loss) if avg_loss != 0 else float('inf')
    out.append(100 - (100/(1+rs)))
    for i in range(period+1, len(closes)):
        ch = closes[i]-closes[i-1]
        gain = max(ch,0); loss = abs(min(ch,0))
        avg_gain = (avg_gain*(period-1)+gain)/period
        avg_loss = (avg_loss*(period-1)+loss)/period
        rs = (avg_gain/avg_loss) if avg_loss != 0 else float('inf')
        out.append(100 - (100/(1+rs)))
    return out

def bollinger(closes, period=20, mult=2):
    if len(closes) < period:
        return ([None]*len(closes), [None]*len(closes), [None]*len(closes))
    out_mid, out_up, out_lo = [], [], []
    for i in range(period-1, len(closes)):
        window = closes[i-period+1:i+1]
        m = mean(window); sd = pstdev(window)
        out_mid.append(m)
        out_up.append(m + mult*sd)
        out_lo.append(m - mult*sd)
    return ( [None]*(period-1) + out_mid,
             [None]*(period-1) + out_up,
             [None]*(period-1) + out_lo )

def atr(highs, lows, closes, period=14):
    if len(closes) < period+1:
        return [None]*len(closes)
    trs = []
    prev_close = closes[0]
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i]-lows[i]
        else:
            tr = max(
                highs[i]-lows[i],
                abs(highs[i]-prev_close),
                abs(lows[i]-prev_close)
            )
        trs.append(tr)
        prev_close = closes[i]
    out = [None]*(period-1)
    cur = sum(trs[:period])/period
    out.append(cur)
    for i in range(period, len(trs)):
        cur = (cur*(period-1)+trs[i])/period
        out.append(cur)
    return out