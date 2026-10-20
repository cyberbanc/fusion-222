"""Small constrained logistic ensemble with purged chronological validation.

Paired LONG/SHORT labels from the same candle stay in one temporal group.
No validation observation is used to fit coefficients. An unvalidated model
can collect shadow outcomes but cannot open an actual paper position.
"""
import hashlib
import math
from .config import CONFIG as C
from .scoring import BLOCKS,score_profile


def sigmoid(x):return 1/(1+math.exp(-max(-35,min(35,x))))


def fit(rows,rules):
    labels=[int(r['exit_reason']=='TAKE_PROFIT') for r in rows]
    prior=(sum(labels)+1)/(len(rows)+2)
    intercept=math.log(prior/(1-prior));side_bias=0.0
    coefficients=[C['scoring_rules']['trend_weights'][k]/100 for k in BLOCKS]
    inputs=[]
    for row in rows:
        sign=1 if row['side']=='LONG' else -1
        inputs.append(([sign*(row['ballot_input']['blocks'][k] or 0.0) for k in BLOCKS],sign))
    for _ in range(rules['epochs']):
        gradients=[0.0]*len(BLOCKS);gi=gs=0.0
        for (values,sign),label in zip(inputs,labels):
            error=sigmoid(intercept+side_bias*sign+sum(c*x for c,x in zip(coefficients,values)))-label
            gi+=error;gs+=error*sign
            for i,x in enumerate(values):gradients[i]+=error*x
        n=len(rows);lr=rules['learning_rate'];intercept-=lr*gi/n
        side_bias-=lr*(gs/n+rules['l2']*side_bias)
        coefficients=[max(0,c-lr*(g/n+rules['l2']*c)) for c,g in zip(coefficients,gradients)]
    total=sum(coefficients)
    weights={k:(100*coefficients[i]/total if total>1e-9 else C['scoring_rules']['trend_weights'][k])
             for i,k in enumerate(BLOCKS)}
    return {'intercept':intercept,'side_bias':side_bias,'coefficients':dict(zip(BLOCKS,coefficients)),
            'weights':weights,'baseline_probability':prior}


def predict(model,row):
    sign=1 if row['side']=='LONG' else -1
    return sigmoid(model['intercept']+model['side_bias']*sign+
        sum(model['coefficients'][k]*sign*(row['ballot_input']['blocks'][k] or 0.0) for k in BLOCKS))


def evaluate(model,rows):
    loss=base_loss=brier=0.0;selected=[];validation=[]
    baseline=model['baseline_probability']
    for row in rows:
        probability=max(1e-9,min(1-1e-9,predict(model,row)))
        label=int(row['exit_reason']=='TAKE_PROFIT')
        loss-=label*math.log(probability)+(1-label)*math.log(1-probability)
        base_loss-=label*math.log(baseline)+(1-label)*math.log(1-baseline)
        brier+=(probability-label)**2
        scoring=score_profile(row['ballot_input'],'LEARNED',model['weights'])
        eligible=scoring['direction']==row['side']
        if eligible:selected.append(row)
        validation.append({'side':row['side'],'score':scoring['score'],'pnl_r':row['pnl_r'],
            'exit_reason':row['exit_reason'],'exit_ts':row['exit_ts'],'selected':eligible,
            'entry_ts':row['entry_ts'],'model_probability':probability})
    n=len(rows);gp=sum(max(r['pnl_r'],0) for r in selected);gl=sum(max(-r['pnl_r'],0) for r in selected)
    return {'logloss':loss/n,'baseline_logloss':base_loss/n,'logloss_improvement':(base_loss-loss)/n,
        'brier_score':brier/n,'selected_trades':len(selected),'selected_net_r':sum(r['pnl_r'] for r in selected),
        'selected_pf':gp/gl if gl else (999.0 if gp else None),'validation_rows':validation}


def paired_groups(rows,now):
    groups={}
    for r in rows:
        if r.get('status')!='CLOSED' or r.get('exit_ts',now+1)>now or not r.get('ballot_input'):continue
        # Both members of a group must have known labels; never split directions.
        groups.setdefault(r['candle_ts'],{})[r['side']]=r
    return [groups[k] for k in sorted(groups) if set(groups[k])=={'LONG','SHORT'}]


def build_model(rows,now,symbol,tf):
    rules=C['learner_rules'];groups=paired_groups(rows,now)[-rules['max_groups']:]
    model={'symbol':symbol,'timeframe_minutes':tf,'created_ts':now,'ready':False,
        'status':'MODEL_WARMUP','observed_groups':len(groups),'train_groups':0,'purged_groups':0,
        'validation_groups':0,'min_train_groups':rules['min_train_groups'],
        'required_validation_groups':rules['holdout_groups'],'weights':C['scoring_rules']['trend_weights'],
        'latest_group_ts':max((r['LONG']['candle_ts'] for r in groups),default=0),
        'validation_rows':[],'method':'nonnegative_logistic_6_blocks_purged_holdout_v1'}
    if len(groups)<rules['min_train_groups']+rules['holdout_groups']:return model
    validation_groups=groups[-rules['holdout_groups']:]
    validation_start=min(r['entry_ts'] for g in validation_groups for r in g.values())
    train_candidates=groups[:-rules['holdout_groups']]
    train_groups=[g for g in train_candidates if max(r['exit_ts'] for r in g.values())<validation_start]
    train=[r for g in train_groups for r in g.values()]
    validation=[r for g in validation_groups for r in g.values()]
    model.update(train_groups=len(train_groups),purged_groups=len(train_candidates)-len(train_groups),
        validation_groups=len(validation_groups),validation_start_ts=validation_start,
        train_max_exit_ts=max((r['exit_ts'] for r in train),default=None))
    if len(train_groups)<rules['min_train_groups']:return model
    positives=sum(r['exit_reason']=='TAKE_PROFIT' for r in train)
    vp=sum(r['exit_reason']=='TAKE_PROFIT' for r in validation)
    model.update(train_positives=positives,validation_positives=vp)
    if min(positives,len(train)-positives)<rules['min_train_positives']:return model
    if min(vp,len(validation)-vp)<rules['min_holdout_positives']:return model
    fitted=fit(train,rules);stats=evaluate(fitted,validation)
    ready=(stats['logloss_improvement']>=rules['min_logloss_improvement'] and
        stats['selected_trades']>=rules['min_validation_trades'] and
        (stats['selected_pf'] or 0)>rules['min_validation_pf'] and stats['selected_net_r']>0)
    model.update(fitted);model.update(stats)
    model.update(ready=ready,status='READY' if ready else 'MODEL_VALIDATION_FAILED',
        train_label_count=len(train),validation_label_count=len(validation),
        train_candle_range=[min(r['candle_ts'] for r in train),max(r['candle_ts'] for r in train)],
        validation_candle_range=[min(r['candle_ts'] for r in validation),max(r['candle_ts'] for r in validation)],
        model_id=f'{symbol}_{tf}:'+hashlib.sha256(str((now,model['weights'],validation_start)).encode()).hexdigest()[:16])
    return model


def get_model(store,symbol,tf,now):
    key=f'learner_{symbol}_{tf}';cached=store.get('meta',key)
    rows=store.rows('trial',account=f'{symbol}_{tf}_LEARNED_SHADOW',status='CLOSED',limit=1600)
    groups=paired_groups(rows,now);last=max((g['LONG']['candle_ts'] for g in groups),default=0)
    if cached:
        if cached['latest_group_ts']==last:return cached
        newer=sum(g['LONG']['candle_ts']>cached['latest_group_ts'] for g in groups)
        if cached.get('model_id') and newer<C['learner_rules']['refit_groups']:return cached
    model=build_model(rows,now,symbol,tf)
    model['latest_group_ts']=last
    store.put('meta',key,model)
    store.put('model',f'{symbol}_{tf}:{last}',model)
    return model
