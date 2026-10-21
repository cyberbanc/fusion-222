import math
import statistics
from .config import CONFIG as C


def clip(x,a=-1,b=1):return max(a,min(b,x))


def ema(values,period):
    out=values[0];a=2/(period+1)
    for x in values[1:]:out=a*x+(1-a)*out
    return out


def atr(bars):
    values=[];prev=None;result=None
    for i,b in enumerate(bars):
        h,l,c=float(b[2]),float(b[3]),float(b[4])
        tr=max(h-l,abs(h-prev) if prev else 0,abs(l-prev) if prev else 0)
        values.append(tr)
        if i==13:result=sum(values)/14
        elif i>13:result=(result*13+tr)/14
        prev=c
    if result is None:raise ValueError('Insufficient ATR history')
    return result


def features(s,previous=None,timeframe=15):
    bars=s['bars_by_timeframe'][str(timeframe)];minutes=s['bars1'];now=s['ts']
    if len(bars)<60 or len(minutes)<60:raise ValueError('Need 60 complete candles')
    for seq,step in [(bars,timeframe*60000),(minutes[-60:],60000)]:
        if any(int(b[0])-int(a[0])!=step for a,b in zip(seq,seq[1:])):
            raise ValueError('Gap in completed candle history')
    if any(int(b[0])//1000+timeframe*60>now for b in bars):raise ValueError('Future candle input')
    a=atr(bars);price=float(s['ticker']['last'])
    if a<=0 or price<=0:raise ValueError('Invalid price or ATR')
    latest=bars[-1];baseline=float(latest[1]);end=int(latest[0])+timeframe*60000
    seq=[float(b[4])-baseline for b in minutes if int(latest[0])<=int(b[0])<end]
    if len(seq)<3:raise ValueError('Missing intrabar price trajectory')
    ds=[b-a for a,b in zip(seq,seq[1:])]
    nz=[abs(x) for x in ds if abs(x)>1e-9]
    typical=max(statistics.median(nz) if nz else a/15,price*.00001)
    move=seq[-1]/typical;momentum=(seq[-1]-seq[0])/typical
    accel=(ds[-1]-ds[-2])/typical
    price_score=.5*math.tanh(move/4)+.35*math.tanh(momentum/4)+.15*math.tanh(accel/4)
    price_p=clip(.5+price_score*.16,.34,.66)
    values=[float(b[4]) for b in minutes[-60:]]
    spread=(ema(values[-30:],9)/ema(values[-50:],21)-1)*10000
    ret=(values[-1]/values[-2]-1)*10000
    book=s['book'];ct=float(s['instrument']['ctVal'])
    bid_n=sum(float(x[0])*float(x[1])*ct for x in book['bids'][:20])
    ask_n=sum(float(x[0])*float(x[1])*ct for x in book['asks'][:20])
    book_i=(bid_n-ask_n)/(bid_n+ask_n) if bid_n+ask_n else 0
    buy=sell=0.0
    for t in s['trades']:
        ts=int(t['ts'])//1000
        if not now-60<=ts<=now:continue
        notional=float(t['px'])*float(t['sz'])*ct
        if t['side']=='buy':buy+=notional
        else:sell+=notional
    flow_i=(buy-sell)/(buy+sell) if buy+sell else 0
    flow_score=clip(spread/15)*.35+clip(ret/10)*.25+flow_i*.20+book_i*.20
    flow_p=clip(.5+flow_score*.15,.35,.65)
    ratios=sorted([r for r in s['ratio'] if int(r[0])//1000<=now],key=lambda r:int(r[0]))
    if not ratios:raise ValueError('Future/unavailable crowd ratio')
    ratio=float(ratios[-1][1]);share=ratio/(1+ratio)
    old=float(ratios[-2][1]) if len(ratios)>1 else ratio
    change=share-old/(1+old);imb=(share-.5)*2
    sign=1 if seq[-1]>=0 else -1
    if abs(imb)>=.70:base=imb*-.18+change*.35+sign*.08
    else:base=imb*.28+change*.40+sign*.08
    funding=float(s['funding']['fundingRate'])
    oi=float(s['oi']['oiUsd']);old_oi=float(previous['oi']['oiUsd']) if previous else None
    oi_change=(oi/old_oi-1) if old_oi and old_oi>0 else None
    # Account share, funding crowding, and OI changes are distinct from taker flow.
    crowd_score=.50*clip(base/.35)-.25*clip(funding/.0003)
    if oi_change is not None:crowd_score+=.25*sign*clip(oi_change/.005)
    else:crowd_score/=.75
    crowd_p=clip(.5+crowd_score*.13,.37,.63)
    raw=.4*price_p+.25*flow_p+.35*crowd_p
    direction_p=.5+(raw-.5)*.75
    bid=float(book['bids'][0][0]);ask=float(book['asks'][0][0])
    return {'score':clip((direction_p-.5)/.10),'direction_probability_long':direction_p,
        'price_probability_long':price_p,'flow_probability_long':flow_p,
        'crowd_probability_long':crowd_p,'atr':a,'atr_pct':a/price*100,
        'stop_fraction':C['stop_atr']*a/price,'spread_fraction':(ask-bid)/((ask+bid)/2),
        'book_imbalance':book_i,'taker_imbalance':flow_i,'long_account_share':share,
        'long_short_ratio':ratio,'funding_rate':funding,'open_interest_usd':oi,
        'oi_change':oi_change,'ratio_ts':int(ratios[-1][0])//1000,
        'candle_ts':int(latest[0])//1000,'timeframe_minutes':timeframe,'price':price,
        'source_timestamps':{'ticker':int(s['ticker']['ts'])//1000,'book':int(book['ts'])//1000,
            'oi':int(s['oi']['ts'])//1000,'ratio':int(ratios[-1][0])//1000,'funding':int(s['funding']['ts'])//1000}}


def calibrate(closed,side,score):
    rows=[x for x in closed if x['side']==side and x.get('exit_reason')!='DATA_GAP']
    rows.sort(key=lambda x:abs(x['score']-score))
    rows=rows[:C['calibration_neighbors']]
    weighted=[(math.exp(-abs(x['score']-score)/.20),x) for x in rows]
    total=sum(w for w,x in weighted)
    prior=C['calibration_prior_count']
    prob=(prior*.25+sum(w for w,x in weighted if x['exit_reason']=='TAKE_PROFIT'))/(prior+total)
    ev=sum(w*x['pnl_r'] for w,x in weighted)/(prior+total)
    return {'probability':prob,'ev_r':ev,'samples':len(rows),'weighted_samples':total,
        'ready':len(rows)>=C['calibration_min_samples'],
        'max_exit_ts':max((x['exit_ts'] for x in rows),default=None),
        'method':'past_closed_shadow_nearest_score_beta_prior'}


def quality(closed,side):
    rows=[r for r in closed if r['side']==side and r.get('selected')]
    rows.sort(key=lambda x:x['exit_ts'],reverse=True)
    recent=rows[:8];q=rows[:30]
    gp=sum(r['pnl_r'] for r in q if r['pnl_r']>0)
    gl=-sum(r['pnl_r'] for r in q if r['pnl_r']<0)
    return {'recent_samples':len(recent),'recent_r':sum(r['pnl_r'] for r in recent),
        'quality_samples':len(q),'quality_pf':gp/gl if gl else (999.0 if gp else None)}


def vwap(book,side,qty,ct):
    levels=book['asks'] if side=='LONG' else book['bids']
    remaining=qty;value=0.0
    for row in levels:
        amount=min(remaining,float(row[1])*ct)
        value+=amount*float(row[0]);remaining-=amount
        if remaining<=1e-10:return value/qty
    raise ValueError('INSUFFICIENT_BOOK_DEPTH')


def make_position(s,f,side,risk,bank,trial=False):
    sign=1 if side=='LONG' else -1;fee=C['fee_rate'];slip=C['adverse_slippage']
    ct=float(s['instrument']['ctVal']);lot=float(s['instrument'].get('lotSz') or 1)
    quote=float(s['book']['asks'][0][0] if side=='LONG' else s['book']['bids'][0][0])
    entry=quote*(1+sign*slip);distance=C['stop_atr']*f['atr']
    cash_risk=risk if trial else C['planned_stop_loss_usd']
    if not cash_risk or cash_risk<=0:raise ValueError('Invalid planned cash risk')
    stop_exec=(entry-sign*distance)*(1-sign*slip)
    per_loss=sign*(entry-stop_exec)+fee*(entry+stop_exec)
    if per_loss<=0:raise ValueError('Invalid stop risk')
    if trial:
        qty=cash_risk/per_loss
    else:
        step=ct*lot;minimum=float(s['instrument'].get('minSz') or lot)*ct
        qty=math.floor(cash_risk/per_loss/step+1e-12)*step
        for _ in range(8):
            if qty<minimum-1e-12:raise ValueError('MIN_CONTRACT_SIZE')
            fill=vwap(s['book'],side,qty,ct)
            if abs(fill/quote-1)>C['max_vwap_impact']:raise ValueError('INSUFFICIENT_BOOK_DEPTH')
            entry=fill*(1+sign*slip)
            stop_exec=(entry-sign*distance)*(1-sign*slip)
            per_loss=sign*(entry-stop_exec)+fee*(entry+stop_exec)
            if qty*per_loss<=cash_risk+1e-9:break
            qty=min(qty-step,math.floor(cash_risk/per_loss/step)*step)
        else:raise ValueError('Invalid cash sizing convergence')
        if C.get('collateral_required',True):
            if qty*entry/C['max_leverage']+qty*entry*fee>bank*.90:
                raise ValueError('CASH_TARGET_LEVERAGE_LIMIT')
        elif bank<cash_risk:
            raise ValueError('INSUFFICIENT_RISK_BUDGET')
    stop=entry-sign*distance;stop_exec=stop*(1-sign*slip)
    atr_stop=stop
    planned_loss=cash_risk
    reward=3*cash_risk if trial else C['target_net_profit_usd']
    if side=='LONG':
        stop=(entry*(1+fee)-planned_loss/qty)/((1-slip)*(1-fee))
        take=(entry*(1+fee)+reward/qty)/((1-slip)*(1-fee))
    else:
        stop=(entry*(1-fee)+planned_loss/qty)/((1+slip)*(1+fee))
        take=(entry*(1-fee)-reward/qty)/((1+slip)*(1+fee))
    if stop<=0 or take<=0:raise ValueError('Invalid stop/target')
    return {'symbol':s['symbol'],'instrument_id':s['instrument_id'],'side':side,
        'entry_ts':s['ts'],'created_ts':s['ts'],'entry_price':entry,'quantity':qty,
        'stop_price':stop,'take_price':take,'risk_usd':planned_loss,
        'planned_reward_usd':reward,'notional_usd':qty*entry,
        'position_sizing':'SHADOW_FIXED_RISK' if trial else C['position_sizing'],
        'requested_notional_usd':None,'requested_stop_loss_usd':cash_risk,
        'target_net_profit_usd':reward,'reference_stake_usd':None if trial else C['reference_stake_usd'],
        'atr_stop_price':atr_stop,'stop_atr_multiple':abs(entry-stop)/f['atr'],
        'margin_usd':qty*entry/C['max_leverage'] if C.get('collateral_required',True) else None,
        'leverage':C['max_leverage'] if C.get('collateral_required',True) else None,
        'synthetic_unfunded':not C.get('collateral_required',True),
        'required_exposure_to_bank':qty*entry/bank if bank>0 else None,
        'funded_at_configured_leverage':qty*entry/C['max_leverage']+qty*entry*fee<=bank*.90,
        'entry_fee':qty*entry*fee,'funding_usd':0.0,'funding_events':[],
        'expires_ts':s['ts']+C['max_hold_seconds'],'last_minute':s['ts']//60*60,
        'next_funding_ts':int(s['funding']['fundingTime'])//1000,
        'score':f['score'],'status':'OPEN','mode':'PAPER','version':C['version'],
        'trial':trial,'max_hold_seconds':C['max_hold_seconds']}


def net_at_price(p,quote):
    sign=1 if p['side']=='LONG' else -1
    execution=quote*(1-sign*C['adverse_slippage'])
    return (sign*p['quantity']*(execution-p['entry_price'])-p['entry_fee']
        -p['quantity']*execution*C['fee_rate']-p['funding_usd'])/p['risk_usd']


def price_for_net_r(p,target_r):
    e=p['entry_price'];fee=C['fee_rate'];slip=C['adverse_slippage']
    amount=(target_r*p['risk_usd']+p['funding_usd'])/p['quantity']
    if p['side']=='LONG':return (e*(1+fee)+amount)/((1-slip)*(1-fee))
    return (e*(1-fee)-amount)/((1+slip)*(1+fee))


def excursion(p,low,high,ts,confirmed=True):
    favourable=net_at_price(p,high if p['side']=='LONG' else low)
    adverse=net_at_price(p,low if p['side']=='LONG' else high)
    p['mfe_upper_r']=max(p.get('mfe_upper_r',0),favourable)
    p['mae_upper_r']=min(p.get('mae_upper_r',0),adverse)
    if confirmed:
        if favourable>p.get('mfe_r',0):p['mfe_r']=favourable;p['mfe_ts']=ts
        p['mae_r']=min(p.get('mae_r',0),adverse)
        hits=p.setdefault('confirmed_target_hits',{})
        for target in [.5,1,1.5,2,2.4,2.5,3]:
            if favourable>=target:hits.setdefault(str(target),ts)
    else:
        p['excursion_order_ambiguous']=True
    p['tp_progress_confirmed']=max(0,p.get('mfe_r',0)/3)
    p['tp_progress_upper']=max(0,p.get('mfe_upper_r',0)/3)
    sign=1 if p['side']=='LONG' else -1
    favourable_quote=high if sign==1 else low
    progress=sign*(favourable_quote-p['entry_price'])/abs(p['take_price']-p['entry_price'])
    p['tp_price_progress_upper']=max(p.get('tp_price_progress_upper',0),progress)
    if confirmed:p['tp_price_progress']=max(p.get('tp_price_progress',0),progress)


def exit_for_bar(p,b):
    ts=int(b[0])//1000
    if ts<p['entry_ts'] or ts<=p['last_minute']:return None
    if ts>=p['expires_ts']:
        return (ts,float(b[1]),'TIMEOUT')
    o,h,l,c=map(float,b[1:5]);long=p['side']=='LONG'
    stop,take=p['stop_price'],p['take_price']
    stop_hit=l<=stop if long else h>=stop
    take_hit=h>=take if long else l<=take
    if stop_hit:
        # Unknown intraminute ordering: stop first, gap through stop uses open.
        px=min(stop,o) if long else max(stop,o)
        return (ts+60,px,'STOP_LOSS_BOTH_TOUCHED' if take_hit else 'STOP_LOSS')
    if take_hit:return (ts+60,take,'TAKE_PROFIT')
    if ts+60>=p['expires_ts']:return (ts+60,c,'TIMEOUT')
    return None


def close_position(p,exit_ts,quote,reason):
    p=dict(p);sign=1 if p['side']=='LONG' else -1
    execution=quote*(1-sign*C['adverse_slippage'])
    exit_fee=p['quantity']*execution*C['fee_rate']
    gross=sign*p['quantity']*(execution-p['entry_price'])
    pnl=gross-p['entry_fee']-exit_fee-p['funding_usd']
    p.update(status='CLOSED',exit_ts=exit_ts,exit_price=execution,exit_reason=reason,
        exit_fee=exit_fee,gross_pnl=gross,pnl=pnl,pnl_r=pnl/p['risk_usd'],
        holding_seconds=exit_ts-p['entry_ts'],
        near_tp_reversal=reason.startswith('STOP_LOSS') and p.get('tp_price_progress',0)>=.8,
        near_tp_reversal_possible=reason.startswith('STOP_LOSS') and p.get('tp_price_progress_upper',0)>=.8,
        missed_2r=p.get('mfe_r',0)>=2 and pnl/p['risk_usd']<2,
        missed_2_5r=p.get('mfe_r',0)>=2.5 and pnl/p['risk_usd']<2.5)
    return p
