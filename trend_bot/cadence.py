"""Explicit experimental PAPER entry policy; no minimum trade quota.

Context candles remain closed and causal. Decision groups use their own 5m clock,
so an hourly context candle can produce several distinct observations.
Calibration labels from different exit policies are never mixed.
"""
import copy
from .config import CONFIG as C
from .strategy import atr,calibrate,quality,make_position,price_for_net_r


def execution_features(s,f):
    out=copy.deepcopy(f)
    bars=s.get('bars_by_timeframe',{}).get('5',[])
    execution_atr=atr(bars) if len(bars)>=14 else f.get('execution_atr',f['atr'])
    price=float(s['ticker']['last'])
    cost=2*C['fee_rate']+2*C['adverse_slippage']+f['spread_fraction']
    distance=max(C['stop_atr']*execution_atr,price*cost/C['max_cost_fraction_of_stop'])
    out.update(context_atr=f['atr'],execution_atr=execution_atr,
        atr=distance/C['stop_atr'],stop_fraction=distance/price,
        execution_cost_fraction_of_stop=cost/(distance/price))
    return out


def data_reason(s,f,timeframe):
    now=s['ts'];ages={k:now-v for k,v in f['source_timestamps'].items()}
    if any(ages[k]>C['max_market_age_seconds'] or ages[k]<-5 for k in ['ticker','book','oi','funding']):
        return 'STALE_MARKET_DATA'
    if ages['ratio']>C['max_ratio_age_seconds'] or ages['ratio']<-5:return 'STALE_MARKET_DATA'
    end=f['candle_ts']+timeframe*60
    if end>now or now-end>timeframe*60+90:return 'STALE_MARKET_DATA'
    if f['spread_fraction']>C['max_spread_fraction']:return 'SPREAD_TOO_WIDE'
    if f['execution_cost_fraction_of_stop']>C['max_cost_fraction_of_stop']+1e-9:return 'VOLATILITY_TOO_LOW'
    return None


def entry_limit(store,account,now):
    if store.count('position',account,status='OPEN'):return 'POSITION_ALREADY_OPEN'
    rows=store.rows('position',account=account,limit=None)
    recent=[p for p in rows if now-3600<p['entry_ts']<=now]
    if len(recent)>=C['max_entries_per_rolling_hour']:return 'HOURLY_ENTRY_LIMIT'
    if rows and now-max(p['entry_ts'] for p in rows)<C['entry_cooldown_seconds']:return 'ENTRY_COOLDOWN'
    return None


def decide(engine,s,f,timeframe,profile):
    if profile not in C.get('scoring_profiles',['CLASSIC']):return
    store=engine.store;now=s['ts'];symbol=s['symbol'];policy=C['entry_policy_id']
    bucket=now//C['decision_interval_seconds']*C['decision_interval_seconds']
    signal_key=f'{policy}:{symbol}:{timeframe}:{bucket}:{profile}'
    if store.get('signal',signal_key):return
    f=execution_features(s,f)
    model=scoring=None;phase=None
    if C.get('scoring_profiles'):
        from .scoring import score_profile
        from .learning import get_model
        model=get_model(store,symbol,timeframe,now) if profile=='LEARNED' else None
        validated=bool(model and model['ready'])
        weights=model['weights'] if validated else None
        scoring=score_profile(f['ballot_input'],profile,weights)
        if model:phase='VALIDATED_LEARNED' if validated else 'FIXED_FALLBACK_UNVALIDATED'
        scoring['learning_phase']=phase
        side=scoring['direction']
        f.update(score=scoring['score'],scoring=scoring,
            trend={**f['trend'],'direction':side,'reason':scoring['reason'],'state':scoring['state']})
        shadow=f'{symbol}_{timeframe}_{profile}_SHADOW_{policy}'
    else:
        side='LONG' if f['score']>=0 else 'SHORT'
        shadow=f'{symbol}_{timeframe}_SHADOW_{policy}'
    closed=[p for p in store.rows('trial',account=shadow,status='CLOSED',limit=1000)
        if p['exit_ts']<=now and p.get('entry_policy_id')==policy]
    # Diagnostics use only already closed outcomes with this exact holding policy.
    cal={d:calibrate(closed,d,f['score']) for d in ['LONG','SHORT']}
    selected=cal.get(side,{'probability':None,'ev_r':None,'samples':0,'ready':False})
    q=quality(closed,side)
    blocked=data_reason(s,f,timeframe)
    reason=blocked
    if not reason and scoring and side=='WAIT':reason=scoring['reason']
    if not reason and not scoring and abs(f['score'])<C['classic_min_abs_score']:reason='SIGNAL_TOO_WEAK'
    reason=reason or 'ALLOWED'
    raw_key=f'{symbol}:{now}'
    if not store.get('source',raw_key):
        raw={k:v for k,v in s.items() if k not in ('bars1','bars_by_timeframe')}
        raw['trades']=[{k:t[k] for k in ['ts','px','sz','side']} for t in s.get('trades',[])]
        raw['bar_references']={tf:[b[0] for b in bars] for tf,bars in s.get('bars_by_timeframe',{}).items()}
        raw['minute_references']=[b[0] for b in s.get('bars1',[])[-60:]]
        store.put('source',raw_key,raw)
    common={'symbol':symbol,'created_ts':now,'candle_ts':bucket,
        'context_candle_ts':f['candle_ts'],'decision_bucket_ts':bucket,
        'timeframe_minutes':timeframe,'signal':side,'features':f,'calibration':cal,
        'quality':q,'reason':reason,'raw_input_key':raw_key,'version':C['version'],
        'profile':profile,'entry_policy_id':policy,'scoring':scoring,'learning_phase':phase}
    store.put('signal',signal_key,common)
    if blocked is None:
        for direction in ['LONG','SHORT']:
            trial=make_position(s,f,direction,1,500,trial=True)
            trial.update(id=signal_key+':'+direction,selected=direction==side,
                candle_ts=bucket,context_candle_ts=f['candle_ts'],timeframe_minutes=timeframe,
                account_id=shadow,profile=profile,entry_policy_id=policy,
                trend=f.get('trend'),scoring=scoring,ballot_input=f.get('ballot_input'),
                model_id=model.get('model_id') if model and model['ready'] else None)
            store.put('trial',trial['id'],trial)
    for account_id in [k for k in C['accounts'] if k.startswith(f'{symbol}_{timeframe}_')]:
        a=store.get('account',account_id);mode=a['direction_mode']
        if scoring and mode!=profile:continue
        account_reason=reason
        if mode=='LONG' and side=='SHORT':account_reason='SHORT_SKIPPED_LONG_ONLY'
        if mode=='SHORT' and side=='LONG':account_reason='LONG_SKIPPED_SHORT_ONLY'
        if account_reason=='ALLOWED':account_reason=entry_limit(store,account_id,now) or 'ALLOWED'
        decision={**common,'account_id':account_id,'source_key':signal_key,
            'reason':account_reason,'probability':selected['probability'],'ev_r':selected['ev_r'],
            'calibration_samples':selected['samples'],'calibration_ready':selected['ready'],
            'executed':False,'mode':'PAPER','position_sizing':C['position_sizing'],
            'target_net_profit_usd':C['target_net_profit_usd'],
            'requested_stop_loss_usd':C['planned_stop_loss_usd'],
            'model_id':model.get('model_id') if model and model['ready'] else None,
            'model_status':model['status'] if model else None,
            'diagnostic_only_gates':['calibration','probability','ev','recent_shadow','quality_pf']}
        if account_reason=='ALLOWED':
            try:
                p=make_position(s,f,side,None,a['bank'])
                p.update(id=account_id+':'+signal_key,account_id=account_id,candle_ts=bucket,
                    context_candle_ts=f['candle_ts'],signal_key=signal_key,timeframe_minutes=timeframe,
                    profile=profile,entry_policy_id=policy,learning_phase=phase,trend=f.get('trend'),
                    scoring=scoring,ballot_input=f.get('ballot_input'),model_id=decision['model_id'])
                store.put('position',p['id'],p)
                cases={}
                for stop_r in C['study_stops_r']:
                    for tp_r in C['study_targets_r']:
                        case=copy.deepcopy(p);case['trial']=True
                        case.update(stop_price=price_for_net_r(p,-stop_r),take_price=price_for_net_r(p,tp_r),
                            study_stop_r=stop_r,study_tp_r=tp_r,planned_reward_usd=tp_r*p['risk_usd'],
                            target_net_profit_usd=tp_r*p['risk_usd'],requested_stop_loss_usd=stop_r*p['risk_usd'])
                        cases[f'SL{stop_r}_TP{tp_r}']=case
                store.put('study',p['id'],{'id':p['id'],'account_id':account_id,'symbol':symbol,
                    'created_ts':now,'status':'OPEN','cases':cases,'note':C['study_note'],
                    'entry_policy_id':policy})
                decision.update(executed=True,position_id=p['id'],risk_usd=p['risk_usd'],
                    entry_price=p['entry_price'],stop_price=p['stop_price'],take_price=p['take_price'])
            except ValueError as exc:decision['reason']=account_reason=str(exc)
        store.put('decision',account_id+':'+signal_key,decision)
        a.update(status='POSITION_OPEN' if store.count('position',account_id,status='OPEN') else account_reason,
            last_signal=side,last_decision_ts=now,last_probability=selected['probability'],
            last_ev_r=selected['ev_r'],calibration_samples=selected['samples'],calibration=cal,
            entry_policy_id=policy,learning_phase=phase)
        if scoring:a.update(trend=f['trend'],scoring=scoring)
        if model:a['learning']={k:v for k,v in model.items() if k!='validation_rows'}
        store.put('account',account_id,a)
