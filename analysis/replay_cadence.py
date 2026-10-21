import copy,importlib,json,sys,zipfile,time
from collections import Counter
from pathlib import Path
name,pkg,archive_path,output_path=sys.argv[1:];mod=importlib.import_module(pkg+'.engine');strategy=importlib.import_module(pkg+'.strategy')
C=importlib.import_module(pkg+'.config').CONFIG;Store=importlib.import_module(pkg+'.storage').Store
z=zipfile.ZipFile(archive_path)
def read(k):return [json.loads(x) for x in z.read(k+'.jsonl').splitlines()]
sources=read('source');bars1=read('candle1');bars_tf=read('candle_tf');snapshots=read('snapshot')
by_min={sym:sorted([r['bar'] for r in bars1 if r['symbol']==sym],key=lambda b:int(b[0])) for sym in C['symbols']}
by_tf={sym:{str(tf):sorted([r['bar'] for r in bars_tf if r['symbol']==sym and r['timeframe_minutes']==tf],key=lambda b:int(b[0])) for tf in [5,15,30,60,240]} for sym in C['symbols']}
funding={sym:{} for sym in C['symbols']}
for s in sources:
 for row in s.get('funding_history',[]):funding[s['symbol']][row['fundingTime']]=row
class ReplayStore(Store):
    # Exit studies do not affect entry decisions or bank; skip their 16x replay cost.
    def put(self,kind,key,value):
        if kind=='study':return
        return super().put(kind,key,value)
store=ReplayStore(':memory:');engine=mod.Engine(store,None)
# Replay only recorded source instants, never invent intervening books or taker flow.
events=[]
for r in snapshots:events.append((r['ts'],0,r))
for r in sources:events.append((r['ts'],1,r))
events.sort(key=lambda x:(x[0],x[1]));start=min(r['ts'] for r in sources);end=max(r['ts'] for r in snapshots)
errors=Counter();done=0
for now,kind,r in events:
 sym=r['symbol'];minutes=[b for b in by_min[sym] if int(b[0])//1000+60<=now]
 if not minutes:continue
 if kind==0:
  s={'symbol':sym,'ts':now,'bars1':minutes[-330:],'ticker':r['ticker'],
     'funding_history':[row for row in funding[sym].values() if int(row['fundingTime'])//1000<=now]}
  engine.process_positions(s);engine.update_accounts(s)
  continue
 s=copy.deepcopy(r);s['bars1']=minutes[-300:]
 s['bars_by_timeframe']={tf:[b for b in bars if int(b[0])//1000+int(tf)*60<=now][-120:] for tf,bars in by_tf[sym].items() if bars}
 s['funding_history']=[row for row in funding[sym].values() if int(row['fundingTime'])//1000<=now]
 for tf in C['timeframes_minutes']:
  try:
   f=strategy.features(s,None,tf)
   if C.get('scoring_profiles'):
    tr=importlib.import_module(pkg+'.trend').trend_state(s,tf)
    ballot=importlib.import_module(pkg+'.scoring').ballot_input(s,tf,f,tr)
    f.update(trend=tr,ballot_input=ballot)
    for p in C['scoring_profiles']:engine.decide(s,f,tf,p)
   else:engine.decide(s,f,tf)
  except (ValueError,KeyError) as exc:errors[str(exc)]+=1
 done+=1
 if done%100==0:print(name,done,'sources',flush=True)
positions=store.rows('position',limit=None);hours=(end-start)/3600
rows=[]
for a in store.rows('account',limit=None):
 ps=[p for p in positions if p['account_id']==a['account_id']];closed=[p for p in ps if p['status']=='CLOSED']
 rows.append({'account':a['account_id'],'entries':len(ps),'entries_per_hour':len(ps)/hours,
   'closed':len(closed),'wins':sum(p['pnl']>0 for p in closed),'equity':a['equity'],
   'pnl':a['equity']-500,'settled_pnl':a['bank']-500,'max_dd':a['max_dd_usd'],
   'tp':sum(p['exit_reason']=='TAKE_PROFIT' for p in closed),
   'synthetic':True})
result={'bot':name,'start_ts':start,'end_ts':end,'hours':hours,'source_records':done,'errors':dict(errors),
 'rows':rows,'decisions':dict(Counter(d['reason'] for d in store.rows('decision',limit=None))),
 'limitation':'Recorded source instants and minute observations only; stop first on ambiguous candles; snapshot tick paths incomplete. Retrospective experiment, no proof of out-of-sample edge, no changes to live history. Historical intra-poll OI changes are not reconstructed: recomputation omits OI change. LEARNED fallback is fixed weights until validation.'}
Path(output_path).write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
