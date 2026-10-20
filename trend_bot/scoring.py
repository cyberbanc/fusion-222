"""Closed-bar indicators, capped family votes, and auditable regime gating."""
import math
from .config import CONFIG as C
from .strategy import atr,ema,clip

BLOCKS=('structure','trend','momentum','vwap','flow','higher')
NAMES={'structure':'Структура и уровни','trend':'Направление тренда',
       'momentum':'Импульс','vwap':'VWAP','flow':'Поток сделок','higher':'Старший таймфрейм'}


def closed_bars(s,tf):
    step=tf*60
    bars=sorted([b for b in s['bars_by_timeframe'].get(str(tf),[])
        if len(b)>8 and str(b[8])=='1' and int(b[0])//1000+step<=s['ts']],key=lambda b:int(b[0]))
    if any(int(b[0])-int(a[0])!=step*1000 for a,b in zip(bars,bars[1:])):return []
    return bars


def vote(x):
    """Symmetric strength buckets; neutral is different from missing data."""
    if x is None:return None
    magnitude=1.0 if abs(x)>=.6 else .5 if abs(x)>=.2 else 0.0
    return math.copysign(magnitude,x) if magnitude else 0.0


def mean(values):
    values=[v for v in values if v is not None]
    return sum(values)/len(values) if values else None


def ema_series(values,period):
    result=[];value=values[0];alpha=2/(period+1)
    for x in values:
        value=alpha*x+(1-alpha)*value;result.append(value)
    return result


def rsi(closes,period=14):
    changes=[b-a for a,b in zip(closes,closes[1:])]
    up=sum(max(x,0) for x in changes[:period])/period
    down=sum(max(-x,0) for x in changes[:period])/period
    for x in changes[period:]:
        up=(up*(period-1)+max(x,0))/period
        down=(down*(period-1)+max(-x,0))/period
    return 100*up/(up+down) if up+down else 50.0


def supertrend(bars,period=10,multiplier=3):
    """Wilder ATR bands, evaluated in time order without rewriting old signals."""
    trs=[];a=None;upper=lower=previous_close=None;direction=1
    for i,b in enumerate(bars):
        high,low,close=map(float,(b[2],b[3],b[4]))
        tr=max(high-low,abs(high-previous_close) if previous_close is not None else 0,
               abs(low-previous_close) if previous_close is not None else 0)
        trs.append(tr)
        if i==period-1:a=sum(trs)/period
        elif i>=period:a=(a*(period-1)+tr)/period
        if a is not None:
            bu=(high+low)/2+multiplier*a;bl=(high+low)/2-multiplier*a
            if upper is None:upper,lower=bu,bl;direction=1 if close>=(high+low)/2 else -1
            else:
                old_upper,old_lower=upper,lower
                upper=bu if bu<old_upper or previous_close>old_upper else old_upper
                lower=bl if bl>old_lower or previous_close<old_lower else old_lower
                if direction<0 and close>upper:direction=1
                elif direction>0 and close<lower:direction=-1
        previous_close=close
    return direction if a is not None else None


def linear_regression(closes,a):
    values=closes[-20:];n=len(values);xbar=(n-1)/2;ybar=sum(values)/n
    sxx=sum((i-xbar)**2 for i in range(n));syy=sum((y-ybar)**2 for y in values)
    sxy=sum((i-xbar)*(y-ybar) for i,y in enumerate(values))
    slope=sxy/sxx;r2=sxy*sxy/(sxx*syy) if syy else 0.0
    return clip(slope*(n-1)/a/3)*r2


def heikin_ashi(bars):
    opened=(float(bars[0][1])+float(bars[0][4]))/2;closed=None;directions=[]
    for b in bars:
        if closed is not None:opened=(opened+closed)/2
        closed=sum(float(b[k]) for k in [1,2,3,4])/4
        directions.append(1 if closed>opened else -1 if closed<opened else 0)
    return sum(directions[-5:])/5


def midpoint(bars,period):
    window=bars[-period:]
    return (max(float(b[2]) for b in window)+min(float(b[3]) for b in window))/2


def ballot_input(s,tf,f,tr):
    bars=closed_bars(s,tf)
    empty={'blocks':dict.fromkeys(BLOCKS),'methods':{},'regime':tr,
           'higher_tf':C['scoring_rules']['higher_timeframes'][str(tf)],'candle_ts':f['candle_ts']}
    if len(bars)<80 or tr.get('atr',0)<=0:return empty
    closes=[float(b[4]) for b in bars];a=tr['atr'];close=closes[-1];methods={}
    # Structure: two consecutive ten-bar windows, entirely in the observed past.
    older,newer=bars[-20:-10],bars[-10:]
    dh=max(float(b[2]) for b in newer)-max(float(b[2]) for b in older)
    dl=min(float(b[3]) for b in newer)-min(float(b[3]) for b in older)
    methods['structure']=mean([clip(dh/a),clip(dl/a)])
    width=tr['resistance']-tr['support']
    methods['channel']=clip(2*(close-(tr['resistance']+tr['support'])/2)/width) if width else 0.0
    methods['ema']=mean([clip((tr['ema_fast']-tr['ema_slow'])/a/1.5),
                         clip(tr['ema_slow_slope_atr']/.2)])
    total_di=tr['plus_di']+tr['minus_di']
    methods['dmi']=clip((tr['plus_di']-tr['minus_di'])/total_di/.3) if total_di else 0.0
    methods['supertrend']=supertrend(bars)
    # Today's cloud is generated 26 candles ago, never using future spans.
    cloud_bars=bars[:-26]
    span_a=(midpoint(cloud_bars,9)+midpoint(cloud_bars,26))/2
    span_b=midpoint(cloud_bars,52)
    cloud_side=1 if close>max(span_a,span_b) else -1 if close<min(span_a,span_b) else 0
    methods['ichimoku']=mean([cloud_side,clip((midpoint(bars,9)-midpoint(bars,26))/a)])
    methods['regression']=linear_regression(closes,a)
    window=bars[-25:]
    high_index=max(range(25),key=lambda i:(float(window[i][2]),i))
    low_index=min(range(25),key=lambda i:(float(window[i][3]),-i))
    methods['aroon']=(high_index-low_index)/24
    methods['heikin_ashi']=heikin_ashi(bars)
    macd=[x-y for x,y in zip(ema_series(closes,12),ema_series(closes,26))]
    methods['macd']=clip((macd[-1]-ema(macd,9))/a/.15)
    methods['roc']=clip((close-closes[-11])/a/2)
    rsi_value=rsi(closes);methods['rsi']=clip((rsi_value-50)/20)
    # UTC session VWAP uses actual aggregate quote/base volume, not OHLC proxies.
    session_start=(int(bars[-1][0])//1000)//86400*86400
    session=[b for b in bars if int(b[0])//1000>=session_start]
    vwap_value=None
    if session and int(session[0][0])//1000==session_start:
        base=sum(float(b[6]) for b in session);quote=sum(float(b[7]) for b in session)
        if base>0 and quote>0:vwap_value=quote/base
    methods['vwap']=clip((close-vwap_value)/a) if vwap_value else None
    signed_volume=volume=0.0
    for before,b in zip(bars[-21:-1],bars[-20:]):
        v=float(b[6]);change=float(b[4])-float(before[4]);volume+=v
        signed_volume+=v*(1 if change>0 else -1 if change<0 else 0)
    methods['obv_flow']=clip(signed_volume/volume/.3) if volume else None
    methods['observed_taker']=f.get('taker_imbalance')
    methods['book']=f.get('book_imbalance')
    higher_tf=C['scoring_rules']['higher_timeframes'][str(tf)]
    higher=closed_bars(s,higher_tf);higher_close_ts=None
    if len(higher)>=60:
        hc=[float(b[4]) for b in higher];ha=atr(higher)
        methods['higher']=mean([clip((ema(hc,20)-ema(hc,50))/ha/1.5),
            clip((ema(hc,50)-ema(hc[:-5],50))/ha/.2)]) if ha else None
        higher_close_ts=int(higher[-1][0])//1000+higher_tf*60
    else:methods['higher']=None
    families={k:vote(v) for k,v in methods.items()}
    blocks={'structure':vote(mean([families['structure'],families['channel']])),
        'trend':vote(mean([families[k] for k in ['ema','dmi','supertrend','ichimoku','regression','aroon','heikin_ashi']])),
        'momentum':vote(mean([families[k] for k in ['macd','roc','rsi']])),
        'vwap':families['vwap'],
        'flow':vote(mean([families['observed_taker'],families['book'],families['obv_flow']])),
        'higher':families['higher']}
    moves=[abs(b-a) for a,b in zip(closes[-21:-1],closes[-20:])]
    efficiency=abs(closes[-1]-closes[-21])/sum(moves) if sum(moves) else 0.0
    return {**empty,'blocks':blocks,'methods':families,'raw_methods':methods,'vwap_price':vwap_value,
        'vwap_anchor_ts':session_start,'rsi':rsi_value,'efficiency_ratio':efficiency,
        'higher_close_ts':higher_close_ts,
        'data_note':'Taker vote uses the observed last-500-trade subset in the prior 60 seconds; not complete exchange CVD. Heikin-Ashi is a vote only; fills use real quotes.'}


def score_profile(ballot,profile,learned_weights=None):
    rules=C['scoring_rules'];tr=ballot['regime'];state=tr.get('state','NO_DATA')
    breakout=state in ['BREAKOUT_UP','BREAKOUT_DOWN']
    weights=dict(learned_weights or (rules['breakout_weights'] if profile=='REGIME' and breakout else rules['trend_weights']))
    blocks=ballot['blocks'];contributions={k:weights[k]*(blocks[k] or 0.0) for k in BLOCKS}
    score=sum(contributions.values());coverage=sum(weights[k] for k in BLOCKS if blocks[k] is not None)
    threshold=rules['breakout_threshold_regime'] if profile=='REGIME' and breakout else rules['threshold']
    direction='LONG' if score>=threshold else 'SHORT' if score<=-threshold else 'WAIT'
    agreeing=sum((blocks[k] or 0)*(1 if direction=='LONG' else -1)>0 for k in BLOCKS) if direction!='WAIT' else 0
    reason=None
    if coverage<rules['min_coverage']:reason='SCORE_DATA_INCOMPLETE'
    elif tr.get('adx') is None:reason='TREND_HISTORY_WARMUP'
    elif not breakout and tr['adx']<=C['trend_rules']['flat_adx_max']:reason='TREND_FLAT_WAIT_BREAKOUT'
    elif not breakout and tr['adx']<C['trend_rules']['trend_adx_min']:reason='TREND_NOT_CONFIRMED'
    elif direction=='WAIT':reason='SCORE_BELOW_THRESHOLD'
    elif breakout and direction!=tr['direction']:reason='SCORE_AGAINST_BREAKOUT'
    elif agreeing<rules['min_agreeing_blocks']:reason='SCORE_CONFLICT'
    if reason:direction='WAIT'
    regime_state=state if breakout or tr.get('adx',0)<25 else 'UP' if direction=='LONG' else 'DOWN' if direction=='SHORT' else 'TRANSITION'
    return {'profile':profile,'score_points':score,'score':score/100,'direction':direction,
        'reason':reason or 'ALLOWED','state':regime_state,'weights':weights,
        'blocks':blocks,'contributions':contributions,'coverage':coverage,'agreeing_blocks':agreeing,
        'threshold':threshold,'block_names':NAMES,'probability_note':'Баллы не являются вероятностью достижения тейка.'}
