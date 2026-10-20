"""Causal EMA/DMI regime and prior-channel breakout classification."""
from .config import CONFIG as C
from .strategy import ema,atr,clip


def directional_series(bars,period=14):
    """Wilder DMI; first DX at period, first ADX at 2*period-1."""
    result=[];trs=[];ups=[];downs=[];dxs=[]
    tr_sum=plus_sum=minus_sum=adx_value=None
    for i,b in enumerate(bars):
        if i==0:
            result.append(None);continue
        prev=bars[i-1];h,l=float(b[2]),float(b[3])
        up=h-float(prev[2]);down=float(prev[3])-l
        trs.append(max(h-l,abs(h-float(prev[4])),abs(l-float(prev[4]))))
        ups.append(up if up>down and up>0 else 0.0)
        downs.append(down if down>up and down>0 else 0.0)
        if i<period:result.append(None);continue
        if i==period:
            tr_sum=sum(trs);plus_sum=sum(ups);minus_sum=sum(downs)
        else:
            tr_sum=tr_sum-tr_sum/period+trs[-1]
            plus_sum=plus_sum-plus_sum/period+ups[-1]
            minus_sum=minus_sum-minus_sum/period+downs[-1]
        plus=100*plus_sum/tr_sum if tr_sum else 0.0
        minus=100*minus_sum/tr_sum if tr_sum else 0.0
        dx=100*abs(plus-minus)/(plus+minus) if plus+minus else 0.0
        dxs.append(dx)
        if len(dxs)==period:adx_value=sum(dxs)/period
        elif len(dxs)>period:adx_value=(adx_value*(period-1)+dx)/period
        result.append({'adx':adx_value,'plus_di':plus,'minus_di':minus})
    return result


def classify_regime(values,rules):
    """Pure decision step, with breakout bounds formed before signal candle."""
    v=values;buffer=v['atr']*rules['breakout_buffer_atr']
    side='WAIT';state='TRANSITION';reason='TREND_NOT_CONFIRMED'
    # A breakout must originate from the previously observed flat regime.
    if v['previous_adx']<=rules['flat_adx_max'] and v['close']>v['resistance']+buffer:
        side='LONG';state='BREAKOUT_UP';reason='ALLOWED'
    elif v['previous_adx']<=rules['flat_adx_max'] and v['close']<v['support']-buffer:
        side='SHORT';state='BREAKOUT_DOWN';reason='ALLOWED'
    elif v['adx']>=rules['trend_adx_min']:
        if (v['close']>v['ema_fast']>v['ema_slow'] and
                v['ema_slow_slope_atr']>=rules['min_slope_atr'] and v['plus_di']>v['minus_di']):
            side='LONG';state='UP';reason='ALLOWED'
        elif (v['close']<v['ema_fast']<v['ema_slow'] and
                v['ema_slow_slope_atr']<=-rules['min_slope_atr'] and v['minus_di']>v['plus_di']):
            side='SHORT';state='DOWN';reason='ALLOWED'
    elif v['adx']<=rules['flat_adx_max']:
        state='FLAT';reason='TREND_FLAT_WAIT_BREAKOUT'
    return {'direction':side,'state':state,'reason':reason,
        'breakout_up_level':v['resistance']+buffer,'breakout_down_level':v['support']-buffer}


def trend_state(s,timeframe):
    rules=C['trend_rules'];step=timeframe*60
    bars=sorted([b for b in s['bars_by_timeframe'][str(timeframe)]
        if len(b)>8 and b[8]=='1' and int(b[0])//1000+step<=s['ts']],key=lambda b:int(b[0]))
    empty={'state':'NO_DATA','direction':'WAIT','reason':'TREND_HISTORY_WARMUP',
        'score':0.0,'method':rules['method'],'candles_used':len(bars)}
    if len(bars)<60:return empty
    if any(int(b[0])-int(a[0])!=step*1000 for a,b in zip(bars,bars[1:])):return empty
    indicators=directional_series(bars,rules['adx_period'])
    if not indicators[-1] or indicators[-1]['adx'] is None:return empty
    a=atr(bars)
    if a<=0:return empty
    closes=[float(b[4]) for b in bars]
    fast=ema(closes,rules['ema_fast']);slow=ema(closes,rules['ema_slow'])
    slope=(slow-ema(closes[:-rules['slope_bars']],rules['ema_slow']))/a
    channel=bars[-rules['channel_bars']-1:-1]
    v={'close':closes[-1],'atr':a,'ema_fast':fast,'ema_slow':slow,
        'ema_slow_slope_atr':slope,'support':min(float(b[3]) for b in channel),
        'resistance':max(float(b[2]) for b in channel),'previous_adx':indicators[-2]['adx'],
        **indicators[-1]}
    decision=classify_regime(v,rules)
    dmi=(v['plus_di']-v['minus_di'])/(v['plus_di']+v['minus_di']) if v['plus_di']+v['minus_di'] else 0.0
    score=clip(.5*clip((fast-slow)/a/3)+.3*clip(slope)+.2*dmi)
    if decision['state']=='BREAKOUT_UP':score=max(.6,score)
    if decision['state']=='BREAKOUT_DOWN':score=min(-.6,score)
    return {**v,**decision,'score':score,'method':rules['method'],'candles_used':len(bars),
        'candle_ts':int(bars[-1][0])//1000,'channel_end_ts':int(channel[-1][0])//1000,
        'channel_bars':rules['channel_bars'],'breakout_buffer_atr':rules['breakout_buffer_atr']}
