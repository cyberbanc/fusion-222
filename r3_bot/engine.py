import concurrent.futures
import copy
import json
import os
import threading
import time
from .config import CONFIG as C, REASONS
from .strategy import features,calibrate,quality,make_position,exit_for_bar,close_position,excursion,net_at_price,price_for_net_r


def advance(p,s):
    """Causal minute/tick replay; ambiguous closing-bar excursions stay bounds."""
    p=copy.deepcopy(p);exit_event=tuple(p['pending_exit']) if p.get('pending_exit') else None
    for b in ([] if exit_event else s['bars1']):
        ts=int(b[0])//1000
        if ts<=p['last_minute']:continue
        if ts!=p['last_minute']+60:
            p['data_gap']=True
            return p
        event=exit_for_bar(p,b)
        if event:
            excursion(p,float(b[3]),float(b[2]),event[0],confirmed=False)
            exit_event=event
            break
        excursion(p,float(b[3]),float(b[2]),ts+60,confirmed=True)
        p['last_minute']=ts
    if not exit_event:
        long=p['side']=='LONG'
        quote=float(s['ticker']['bidPx'] if long else s['ticker']['askPx'])
        if quote<=0:raise ValueError('Missing executable market quote')
        # Only observed ticks are used in the partial entry/current minute.
        excursion(p,quote,quote,s['ts'],confirmed=True)
        if (quote<=p['stop_price'] if long else quote>=p['stop_price']):
            exit_event=(s['ts'],quote,'STOP_LOSS')
        elif (quote>=p['take_price'] if long else quote<=p['take_price']):
            exit_event=(s['ts'],p['take_price'],'TAKE_PROFIT')
        elif s['ts']>=p['expires_ts']:
            exit_event=(s['ts'],quote,'TIMEOUT')
    end=exit_event[0] if exit_event else s['ts']
    known={int(e['ts']) for e in p['funding_events']}
    for row in s['funding_history']:
        ts=int(row['fundingTime'])//1000
        if p['entry_ts']<ts<=end and ts not in known:
            rate=float(row.get('realizedRate') or row['fundingRate'])
            sign=1 if p['side']=='LONG' else -1
            # Funding notional uses nearest already-completed minute close.
            prior=[b for b in s['bars1'] if int(b[0])//1000+60<=ts]
            px=float(prior[-1][4]) if prior else p['entry_price']
            amount=sign*p['quantity']*px*rate
            p['funding_events'].append({'ts':ts,'rate':rate,'mark_proxy':px,'amount':amount})
            p['funding_usd']+=amount;known.add(ts)
    if p['entry_ts']<p['next_funding_ts']<=end and p['next_funding_ts'] not in known:
        p['funding_pending']=True
        # Persist the economic exit while waiting for the published funding rate.
        if exit_event:p['pending_exit']=list(exit_event)
        return p
    p.pop('funding_pending',None)
    if p.get('pending_exit'):exit_event=tuple(p.pop('pending_exit'))
    p.pop('data_gap',None)
    if exit_event:
        if exit_event[2]=='TAKE_PROFIT':
            # The TP touch is confirmed; extremes after exit remain bounds.
            q=exit_event[1];excursion(p,q,q,exit_event[0],confirmed=True)
        return close_position(p,*exit_event)
    return p


class Engine:
    def __init__(self,store,market):
        self.store=store;self.market=market;self.stop=threading.Event()
        now=int(time.time())
        with store.transaction():
            if not store.get('meta','started'):
                store.put('meta','started',{'ts':now,'started_ts':now})
            store.put('meta','config',C)
            for key in C['accounts']:
                if store.get('account',key):continue
                symbol,tf,mode=key.split('_')
                store.put('account',key,{'account_id':key,'symbol':symbol,'direction_mode':mode,
                    'timeframe_minutes':int(tf),
                    'initial_bank':C['initial_bank'],'bank':C['initial_bank'],'equity':C['initial_bank'],
                    'peak_equity':C['initial_bank'],'max_dd_usd':0.0,'max_dd_pct':0.0,
                    'started_ts':now,'created_ts':now,'status':'CALIBRATION_WARMUP',
                    'trades':0,'wins':0,'losses':0,'gross_profit':0.0,'gross_loss':0.0,
                    'fees_usd':0.0,'funding_usd':0.0,'near_tp_reversals':0,
                    'missed_2r':0,'missed_2_5r':0,'ambiguous_excursions':0})

    def process_positions(self,s):
        symbol=s['symbol']
        for kind in ['trial','position']:
            for p in self.store.rows(kind,symbol=symbol,status='OPEN',limit=None):
                updated=advance(p,s)
                if updated==p:continue
                key=p['id'];self.store.put(kind,key,updated)
                if kind=='position' and updated['status']=='CLOSED':
                    a=self.store.get('account',p['account_id']);pnl=updated['pnl']
                    a['bank']+=pnl;a['trades']+=1
                    a['wins']+=int(pnl>0);a['losses']+=int(pnl<0)
                    a['gross_profit']+=max(pnl,0);a['gross_loss']+=max(-pnl,0)
                    a['fees_usd']+=updated['entry_fee']+updated['exit_fee']
                    a['funding_usd']+=updated['funding_usd']
                    for field,source in [('near_tp_reversals','near_tp_reversal'),('missed_2r','missed_2r'),
                            ('missed_2_5r','missed_2_5r'),('ambiguous_excursions','excursion_order_ambiguous')]:
                        a[field]+=int(bool(updated.get(source)))
                    updated['bank_after']=a['bank'];self.store.put(kind,key,updated)
                    self.store.put('account',a['account_id'],a)
                    self.store.event('INFO','Позиция закрыта',account_id=a['account_id'],symbol=symbol,
                        position_id=key,pnl=pnl,reason=updated['exit_reason'])
        for study in self.store.rows('study',symbol=symbol,status='OPEN',limit=None):
            for key,p in study['cases'].items():
                if p['status']=='OPEN':study['cases'][key]=advance(p,s)
            if all(p['status']=='CLOSED' for p in study['cases'].values()):study['status']='CLOSED'
            self.store.put('study',study['id'],study)

    def update_accounts(self,s):
        for account_id in [k for k in C['accounts'] if k.startswith(s['symbol']+'_')]:
            a=self.store.get('account',account_id)
            positions=self.store.rows('position',account=account_id,status='OPEN')
            unrealized=0.0
            for p in positions:
                quote=float(s['ticker']['bidPx'] if p['side']=='LONG' else s['ticker']['askPx'])
                unrealized+=net_at_price(p,quote)*p['risk_usd']
            equity=a['bank']+unrealized
            a['equity']=equity;a['unrealized_pnl']=unrealized;a['updated_ts']=s['ts']
            a['peak_equity']=max(a['peak_equity'],equity)
            dd=a['peak_equity']-equity
            a['max_dd_usd']=max(a['max_dd_usd'],dd)
            a['max_dd_pct']=max(a['max_dd_pct'],dd/a['peak_equity']*100)
            a['open_positions']=len(positions)
            self.store.put('account',account_id,a)
            key=f"{account_id}:{s['ts']//60*60}"
            self.store.put('equity',key,{'account_id':account_id,'symbol':s['symbol'],
                'ts':s['ts'],'bank':a['bank'],'equity':equity,'drawdown_usd':dd,
                'drawdown_pct':dd/a['peak_equity']*100})

    def process(self,s):
        now=s['ts'];symbol=s['symbol'];previous=self.store.get('meta','latest_'+symbol)
        all_features={tf:features(s,previous,tf) for tf in C['timeframes_minutes']}
        with self.store.transaction():
            self.process_positions(s)
            self.update_accounts(s)
            compact={'symbol':symbol,'ts':now,'ticker':s['ticker'],'oi':s['oi'],
                'funding':s['funding'],'ratio':s['ratio'][:3],'features':all_features,
                'source':'OKX_PUBLIC'}
            self.store.put('snapshot',f'{symbol}:{now//60*60}',compact)
            self.store.put('meta','latest_'+symbol,compact)
            last_saved=self.store.get('meta','last_candle_'+symbol,{}).get('ts',0)
            for bar in s['bars1']:
                if int(bar[0])//1000<=last_saved:continue
                self.store.put('candle1',f'{symbol}:{bar[0]}',{'symbol':symbol,
                    'ts':int(bar[0])//1000,'bar':bar,'source':'OKX_PUBLIC'})
            self.store.put('meta','last_candle_'+symbol,{'ts':int(s['bars1'][-1][0])//1000})
            for tf,bars in s['bars_by_timeframe'].items():
                last=self.store.get('meta',f'last_tf_{symbol}_{tf}',{}).get('ts',0)
                for bar in bars:
                    if int(bar[0])//1000<=last:continue
                    self.store.put('candle_tf',f'{symbol}:{tf}:{bar[0]}',{'symbol':symbol,
                        'ts':int(bar[0])//1000,'timeframe_minutes':int(tf),'bar':bar})
                self.store.put('meta',f'last_tf_{symbol}_{tf}',{'ts':int(bars[-1][0])//1000})
            for tf,f in all_features.items():self.decide(s,f,tf)

    def decide(self,s,f,timeframe):
            now=s['ts'];symbol=s['symbol']
            signal_key=f"{symbol}:{timeframe}:{f['candle_ts']}"
            if self.store.get('signal',signal_key):return
            ages={k:now-v for k,v in f['source_timestamps'].items()}
            data_reason=None
            if any(ages[k]>C['max_market_age_seconds'] or ages[k]<-5 for k in ['ticker','book','oi','funding']):
                data_reason='STALE_MARKET_DATA'
            elif ages['ratio']>C['max_ratio_age_seconds']:data_reason='STALE_MARKET_DATA'
            elif now-(f['candle_ts']+timeframe*60)>120:data_reason='AWAIT_NEXT_BAR'
            elif f['spread_fraction']>C['max_spread_fraction']:data_reason='SPREAD_TOO_WIDE'
            elif (2*C['fee_rate']+2*C['adverse_slippage']+f['spread_fraction'])/f['stop_fraction']>C['max_cost_fraction_of_stop']:
                data_reason='VOLATILITY_TOO_LOW'
            closed=[x for x in self.store.rows('trial',account=f'{symbol}_{timeframe}_SHADOW',status='CLOSED',limit=1000)
                if x['exit_ts']<=now]
            cal={side:calibrate(closed,side,f['score']) for side in ['LONG','SHORT']}
            ready=all(v['ready'] for v in cal.values())
            if ready and max(cal['LONG']['ev_r'],cal['SHORT']['ev_r'])>=0:
                side=max(cal,key=lambda k:cal[k]['ev_r'])
            else:side='LONG' if f['score']>=0 else 'SHORT'
            selected=cal[side];q=quality(closed,side)
            reason=data_reason
            if not reason and not selected['ready']:reason='CALIBRATION_WARMUP'
            if not reason and selected['probability']<C['min_probability']:reason='PROBABILITY_BELOW_53'
            if not reason and selected['ev_r']<C['min_ev_r']:reason='EV_BELOW_MINIMUM'
            if not reason and q['recent_samples']>=8 and q['recent_r']<=-3:reason='SHADOW_RECENT_DEGRADED'
            if not reason and q['quality_samples']>=10 and (q['quality_pf'] or 0)<.85:reason='QUALITY_PF_BELOW_MINIMUM'
            reason=reason or 'ALLOWED'
            # Store original raw inputs once per symbol/decision, not six copies.
            raw_key=f"{symbol}:{now}"
            if not self.store.get('source',raw_key):
                raw={k:v for k,v in s.items() if k not in ('bars1','bars_by_timeframe')}
                raw['trades']=[{k:t[k] for k in ['ts','px','sz','side']} for t in s['trades']]
                raw['bar_references']={tf:[b[0] for b in bars] for tf,bars in s['bars_by_timeframe'].items()}
                raw['minute_references']=[b[0] for b in s['bars1'][-60:]]
                self.store.put('source',raw_key,raw)
            self.store.put('signal',signal_key,{'symbol':symbol,'created_ts':now,
                'candle_ts':f['candle_ts'],'timeframe_minutes':timeframe,'signal':side,'features':f,'calibration':cal,
                'quality':q,'reason':reason,'raw_input_key':raw_key,'version':C['version']})
            # Bidirectional shadow trials train barrier probabilities without
            # spending money or pretending the old direction probability is TP probability.
            if data_reason is None:
                for direction in ['LONG','SHORT']:
                    trial=make_position(s,f,direction,1,500,trial=True)
                    trial.update(id=signal_key+':'+direction,selected=direction==side,
                        candle_ts=f['candle_ts'],timeframe_minutes=timeframe,
                        account_id=f'{symbol}_{timeframe}_SHADOW')
                    self.store.put('trial',trial['id'],trial)
            for account_id in [k for k in C['accounts'] if k.startswith(f'{symbol}_{timeframe}_')]:
                a=self.store.get('account',account_id);account_reason=reason
                mode=a['direction_mode']
                if mode=='LONG' and side=='SHORT':account_reason='SHORT_SKIPPED_LONG_ONLY'
                if mode=='SHORT' and side=='LONG':account_reason='LONG_SKIPPED_SHORT_ONLY'
                if account_reason=='ALLOWED' and self.store.count('position',account_id,status='OPEN'):
                    account_reason='POSITION_ALREADY_OPEN'
                decision={'account_id':account_id,'symbol':symbol,'created_ts':now,
                    'timeframe_minutes':timeframe,
                    'candle_ts':f['candle_ts'],'signal':side,'reason':account_reason,
                    'probability':selected['probability'],'ev_r':selected['ev_r'],
                    'calibration_samples':selected['samples'],'calibration_ready':selected['ready'],
                    'calibration':cal,'quality':q,'features':f,'source_key':signal_key,
                    'executed':False,'version':C['version'],'mode':'PAPER',
                    'position_sizing':C['position_sizing'],
                    'requested_notional_usd':C['fixed_notional_usd']}
                if account_reason=='ALLOWED':
                    try:
                        p=make_position(s,f,side,None,a['bank'])
                        p.update(id=account_id+':'+str(f['candle_ts']),account_id=account_id,
                            candle_ts=f['candle_ts'],signal_key=signal_key,timeframe_minutes=timeframe)
                        self.store.put('position',p['id'],p)
                        cases={}
                        for stop_r in C['study_stops_r']:
                            for tp_r in C['study_targets_r']:
                                case=copy.deepcopy(p);case['trial']=True
                                case['stop_price']=price_for_net_r(p,-stop_r)
                                case['take_price']=price_for_net_r(p,tp_r)
                                case['study_stop_r']=stop_r;case['study_tp_r']=tp_r
                                cases[f'SL{stop_r}_TP{tp_r}']=case
                        self.store.put('study',p['id'],{'id':p['id'],'account_id':account_id,
                            'symbol':symbol,'created_ts':now,'status':'OPEN','cases':cases,
                            'note':C['study_note']})
                        decision.update(executed=True,position_id=p['id'],risk_usd=p['risk_usd'],
                            entry_price=p['entry_price'],stop_price=p['stop_price'],take_price=p['take_price'])
                    except ValueError as exc:
                        account_reason=str(exc);decision['reason']=account_reason
                self.store.put('decision',account_id+':'+str(f['candle_ts']),decision)
                a['status']='POSITION_OPEN' if decision['executed'] else account_reason
                a['last_signal']=side;a['last_decision_ts']=now;a['last_probability']=selected['probability']
                a['last_ev_r']=selected['ev_r'];a['calibration_samples']=selected['samples']
                a['calibration']=cal;self.store.put('account',account_id,a)

    def once(self):
        started=int(time.time());errors={}
        def fetch(symbol):
            rows=self.store.rows('position',symbol=symbol,status='OPEN',limit=None)
            rows+=self.store.rows('trial',symbol=symbol,status='OPEN',limit=None)
            studies=self.store.rows('study',symbol=symbol,status='OPEN',limit=None)
            for study in studies:rows.extend(p for p in study['cases'].values() if p['status']=='OPEN')
            after=min((p['last_minute'] for p in rows),default=None)
            return self.market.snapshot(symbol,after)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures={pool.submit(fetch,symbol):symbol for symbol in C['symbols']}
            for future in concurrent.futures.as_completed(futures):
                symbol=futures[future]
                try:self.process(future.result())
                except Exception as exc:
                    errors[symbol]=type(exc).__name__+': '+str(exc)[:240]
                    self.store.event('ERROR','Ошибка рыночных данных',symbol=symbol,error=errors[symbol])
        now=int(time.time())
        self.store.put('meta','runtime',{'ts':now,'last_poll_ts':now,'poll_started_ts':started,
            'worker_enabled':True,'paper_only':True,'leader':self.store.leader,
            'errors':errors,'status':'DEGRADED' if errors else 'ONLINE','version':C['version']})
        return errors

    def run(self):
        waiting_logged=False
        while not self.stop.is_set():
            if self.store.acquire_worker():break
            if not waiting_logged:
                self.store.event('INFO','Ожидание передачи обработки рынка от предыдущего экземпляра')
                waiting_logged=True
            self.stop.wait(5)
        while not self.stop.is_set():
            try:self.once()
            except Exception as exc:
                print(json.dumps({'error':type(exc).__name__,'message':str(exc)[:240]}),flush=True)
            self.stop.wait(float(os.getenv('POLL_SECONDS','20')))
