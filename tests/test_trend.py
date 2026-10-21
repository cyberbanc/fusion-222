import copy
import io
import json
import math
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from trend_bot.config import CONFIG as C
from trend_bot.storage import Store,TABLE
from trend_bot.engine import Engine,advance
from trend_bot.strategy import make_position,close_position,calibrate,price_for_net_r
from trend_bot.trend import trend_state
from trend_bot.scoring import BLOCKS,ballot_input,score_profile,closed_bars
from trend_bot.learning import build_model,paired_groups,predict
from trend_bot.server import history_archive,summary


def market():
    now=1800000000
    return {'ts':now,'symbol':'SOL','instrument_id':'SOL-USDT-SWAP',
        'instrument':{'ctVal':'1','lotSz':'.001','minSz':'.001'},
        'book':{'asks':[['100.01','100000']], 'bids':[['99.99','100000']]},
        'ticker':{'last':'100','bidPx':'99.99','askPx':'100.01'},
        'funding':{'fundingTime':'1800028800000'},'funding_history':[],
        'bars1':[],'trades':[],'bars_by_timeframe':{}}


def ballot(side=1,state='UP'):
    return {'blocks':dict.fromkeys(BLOCKS,float(side)),
        'regime':{'state':state,'direction':'LONG' if side>0 else 'SHORT',
        'adx':30 if state in ['UP','DOWN'] else 15},'candle_ts':1799999100}


def inputs(side=1,state='UP'):
    s=market();b=ballot(side,state)
    f={'atr':.8,'score':.2,'candle_ts':s['ts']//900*900-900,
        'spread_fraction':.0002,'stop_fraction':.012,
        'source_timestamps':dict.fromkeys(['ticker','book','oi','funding','ratio'],s['ts']),
        'trend':b['regime'],'ballot_input':b}
    return s,f


def closed_trials(groups=360,horizon=30):
    rows=[]
    for i in range(groups):
        sign=1 if i%2 else -1;b=ballot(sign,'UP' if sign>0 else 'DOWN')
        candle=100000+i*60
        for side,d in [('LONG',1),('SHORT',-1)]:
            win=(d==sign)
            rows.append({'status':'CLOSED','side':side,'candle_ts':candle,
                'entry_ts':candle+60,'exit_ts':candle+60+horizon,'score':sign,
                'exit_reason':'TAKE_PROFIT' if win else 'STOP_LOSS','pnl_r':3 if win else -1,
                'ballot_input':b,'selected':win})
    return rows


class AccountAndExecutionTests(unittest.TestCase):
    def test_24_accounts_two_coins_four_timeframes_three_scoring_profiles(self):
        store=Store(':memory:');Engine(store,None)
        accounts=store.rows('account',limit=None)
        self.assertEqual(len(accounts),24)
        self.assertEqual({a['direction_mode'] for a in accounts},{'FIXED','REGIME','LEARNED'})
        self.assertEqual({a['timeframe_minutes'] for a in accounts},{5,15,30,60})
        self.assertEqual(len({a['started_ts'] for a in accounts}),1)
        self.assertTrue(all(a['bank']==500 for a in accounts))
        self.assertNotEqual(TABLE,'fusion_r3_v1_records')

    def test_both_directions_have_exact_22_profit_and_22_over_3_stop(self):
        for side in ['LONG','SHORT']:
            p=make_position(market(),{'atr':.8,'score':.5},side,None,500)
            for price,reason,expected in [(p['take_price'],'TAKE_PROFIT',22),(p['stop_price'],'STOP_LOSS',-22/3)]:
                result=close_position(p,p['entry_ts']+60,price,reason)
                self.assertAlmostEqual(result['pnl'],expected,places=9)
            self.assertIsNone(p['margin_usd']);self.assertTrue(p['synthetic_unfunded'])

    def test_leverage_limit_cannot_silently_shrink_goal(self):
        p=make_position(market(),{'atr':.05,'score':.5},'LONG',None,500)
        self.assertEqual(p['planned_reward_usd'],22)
        self.assertIsNone(p['margin_usd'])
        self.assertFalse(p['funded_at_configured_leverage'])

    def test_profiles_have_separate_decisions_shadows_and_actual_accounts(self):
        store=Store(':memory:');engine=Engine(store,None);s,f=inputs()
        estimate={'ready':True,'probability':.7,'ev_r':1,'samples':15}
        with patch('trend_bot.engine.calibrate',return_value=estimate):
            for profile in ['FIXED','REGIME']:engine.decide(s,f,15,profile)
            engine.decide(s,f,15,'FIXED')
        self.assertEqual({p['account_id'] for p in store.rows('position')},{'SOL_15_FIXED','SOL_15_REGIME'})
        self.assertEqual(store.count('decision'),2);self.assertEqual(store.count('trial'),4)
        self.assertEqual(store.count('study'),2)
        for study in store.rows('study'):
            self.assertEqual(len(study['cases']),16)

    def test_unvalidated_fallback_is_explicit_and_never_claims_learned_weights(self):
        store=Store(':memory:');engine=Engine(store,None);s,f=inputs()
        with patch('trend_bot.engine.calibrate',return_value={'ready':True,'probability':.9,'ev_r':2,'samples':15}):
            engine.decide(s,f,15,'LEARNED')
        self.assertEqual(store.count('position'),1)
        self.assertEqual(store.rows('decision')[0]['learning_phase'],'FIXED_FALLBACK_UNVALIDATED')
        self.assertIsNone(store.rows('position')[0]['model_id'])
        self.assertEqual(store.rows('decision')[0]['model_status'],'MODEL_WARMUP')
        self.assertEqual(store.count('trial'),2)
        self.assertEqual(store.get('account','SOL_15_LEARNED')['learning']['observed_groups'],0)

    def test_calibration_does_not_use_other_profiles(self):
        store=Store(':memory:');engine=Engine(store,None);s,f=inputs()
        for i in range(30):
            store.put('trial',str(i),{'account_id':'SOL_15_FIXED_SHADOW','status':'CLOSED',
                'side':'LONG','score':1,'pnl_r':3,'exit_reason':'TAKE_PROFIT',
                'created_ts':i,'exit_ts':s['ts']-1,'selected':True})
        engine.decide(s,f,15,'REGIME')
        decision=store.rows('decision')[0]
        self.assertEqual(decision['calibration_samples'],0)
        self.assertEqual(decision['reason'],'ALLOWED')
        self.assertFalse(decision['calibration_ready'])

    def test_15_closed_observations_still_required_per_direction(self):
        rows=[{'side':'LONG','score':.5,'pnl_r':3,'exit_reason':'TAKE_PROFIT','exit_ts':i} for i in range(15)]
        self.assertFalse(calibrate(rows[:14],'LONG',.5)['ready'])
        self.assertTrue(calibrate(rows,'LONG',.5)['ready'])
        self.assertFalse(calibrate(rows,'SHORT',.5)['ready'])

    def test_export_contains_all_records_and_models(self):
        store=Store(':memory:');Engine(store,None)
        for i in range(321):store.put('decision',str(i),{'created_ts':i,'account_id':'SOL_15_FIXED','executed':False})
        store.put('model','m',{'created_ts':1,'ready':False})
        archive,counts=history_archive(store)
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip());self.assertEqual(len(z.read('decision.jsonl').splitlines()),321)
            self.assertEqual(counts['account'],24);self.assertEqual(counts['model'],1)
            self.assertIn('model.jsonl',z.namelist())
            self.assertIn('160',z.read('README_RU.txt').decode())
        archive.close()

    def test_partial_entry_minute_does_not_replay_past_extremes(self):
        s=market();p=make_position(s,{'atr':.8,'score':1},'LONG',None,500)
        s['ts']+=60;start=p['entry_ts']//60*60
        s['bars1']=[[str(start*1000),'100','110','90','100','1','1','100','1']]
        self.assertEqual(advance(p,s)['status'],'OPEN')

    def test_same_minute_both_barriers_is_stop_first(self):
        s=market();p=make_position(s,{'atr':.8,'score':1},'LONG',None,500)
        minute=p['last_minute']+60;s['ts']=minute+60
        s['bars1']=[[str(minute*1000),'100',str(p['take_price']+1),str(p['stop_price']-1),'100','1','1','100','1']]
        result=advance(p,s)
        self.assertEqual(result['exit_reason'],'STOP_LOSS_BOTH_TOUCHED')
        self.assertTrue(result['excursion_order_ambiguous'])

    def test_eighty_percent_reversal_and_exit_study_survive(self):
        s=market();p=make_position(s,{'atr':.8,'score':1},'LONG',None,500)
        smaller=copy.deepcopy(p);smaller['take_price']=price_for_net_r(p,2)
        minute=p['last_minute']+60;high=p['entry_price']+.85*(p['take_price']-p['entry_price'])
        s['ts']=minute+60;s['ticker'].update(bidPx=str(high),askPx=str(high+.01))
        s['bars1']=[[str(minute*1000),'100',str(high),str(p['entry_price']-.05),str(high),'1','1','100','1']]
        p=advance(p,s);alt=advance(smaller,s)
        self.assertEqual(p['status'],'OPEN');self.assertEqual(alt['exit_reason'],'TAKE_PROFIT')
        minute+=60;s['ts']=minute+60
        s['bars1']=[[str(minute*1000),'100','100',str(p['stop_price']-.01),str(p['stop_price']),'1','1','100','1']]
        p=advance(p,s);self.assertTrue(p['near_tp_reversal']);self.assertTrue(p['missed_2r'])


class ScoreTests(unittest.TestCase):
    def test_fixed_and_regime_weight_sums_and_thresholds(self):
        b=ballot();self.assertEqual(score_profile(b,'FIXED')['direction'],'LONG')
        b=ballot(-1,'DOWN');self.assertEqual(score_profile(b,'REGIME')['direction'],'SHORT')
        b=ballot(1,'BREAKOUT_UP')
        fixed=score_profile(b,'FIXED');regime=score_profile(b,'REGIME')
        self.assertEqual(fixed['threshold'],25);self.assertEqual(regime['threshold'],25)
        self.assertEqual(regime['weights']['flow'],25)
        self.assertEqual(sum(regime['weights'].values()),100)

    def test_missing_data_does_not_get_renormalized_to_strong_vote(self):
        b=ballot();b['blocks']['structure']=None;b['blocks']['momentum']=None
        result=score_profile(b,'FIXED')
        self.assertEqual(result['score_points'],65)
        self.assertEqual(result['coverage'],65);self.assertEqual(result['reason'],'SCORE_DATA_INCOMPLETE')

    def test_conflicting_votes_do_not_automatically_mean_flat(self):
        b=ballot();b['blocks']={'structure':1,'trend':-1,'momentum':1,'vwap':-1,'flow':1,'higher':-1}
        result=score_profile(b,'FIXED')
        self.assertEqual(result['score_points'],0)
        self.assertEqual(result['reason'],'SCORE_BELOW_THRESHOLD')
        self.assertNotEqual(result['state'],'FLAT')

    def test_flat_and_transition_require_structure_and_momentum_confirmation(self):
        for adx,state,reason in [(15,'FLAT','TREND_FLAT_WAIT_BREAKOUT'),(23,'TRANSITION','TREND_NOT_CONFIRMED')]:
            b=ballot(1,state);b['regime']['adx']=adx
            self.assertEqual(score_profile(b,'FIXED')['direction'],'LONG')
            b['blocks']['momentum']=-1
            self.assertEqual(score_profile(b,'FIXED')['reason'],'TREND_NOT_CONFIRMED')

    def test_breakout_and_ballots_must_agree(self):
        b=ballot(-1,'BREAKOUT_UP');b['regime']['direction']='LONG'
        self.assertEqual(score_profile(b,'REGIME')['reason'],'SCORE_AGAINST_BREAKOUT')

    def test_closed_bar_indicators_and_higher_context_have_no_future_input(self):
        tf=15;now=1800000000
        def bars(t):
            start=(now//(t*60))*t*60-120*t*60
            return [[str((start+i*t*60)*1000),str(100+i*.1),str(100.2+i*.1),str(99.8+i*.1),str(100+i*.1),'1','1',str(100+i*.1),'1'] for i in range(120)]
        s={'ts':now,'bars_by_timeframe':{'15':bars(15),'60':bars(60)}}
        tr=trend_state(s,tf);f={'candle_ts':tr['candle_ts'],'taker_imbalance':.4,'book_imbalance':.3}
        before=ballot_input(s,tf,f,tr)
        self.assertGreater(before['blocks']['trend'],0)
        self.assertLessEqual(before['higher_close_ts'],now)
        for t in [15,60]:s['bars_by_timeframe'][str(t)].append([str(now*1000),'999','1000','1','999','1','1','999','1'])
        self.assertEqual(ballot_input(s,tf,f,trend_state(s,tf)),before)


class LearningTests(unittest.TestCase):
    def test_future_labels_and_incomplete_pairs_are_excluded(self):
        rows=closed_trials(3);now=max(r['exit_ts'] for r in rows)-1
        self.assertEqual(len(paired_groups(rows,now)),2)
        self.assertEqual(len(paired_groups(rows[:-1],now+1000)),2)

    def test_not_enough_groups_cannot_train(self):
        model=build_model(closed_trials(100),999999,'SOL',5)
        self.assertFalse(model['ready']);self.assertEqual(model['status'],'MODEL_WARMUP')

    def test_purge_removes_training_outcomes_overlapping_validation(self):
        rows=closed_trials(300,horizon=2400)
        model=build_model(rows,999999,'SOL',5)
        self.assertGreater(model['purged_groups'],0)
        self.assertLess(model['train_max_exit_ts'],model['validation_start_ts'])

    def test_model_genuinely_fits_and_validates_on_next_eighty_groups(self):
        model=build_model(closed_trials(300),999999,'SOL',15)
        self.assertTrue(model['ready'])
        self.assertEqual(model['validation_groups'],80)
        self.assertGreater(model['logloss_improvement'],0)
        self.assertGreater(model['selected_net_r'],0)
        self.assertAlmostEqual(sum(model['weights'].values()),100)
        self.assertTrue(all(w>=0 for w in model['weights'].values()))
        self.assertLess(model['train_candle_range'][1],model['validation_candle_range'][0])

    def test_validation_outcomes_never_change_fitted_coefficients(self):
        rows=closed_trials(300);first=build_model(rows,999999,'SOL',15)
        changed=copy.deepcopy(rows)
        cutoff=first['validation_candle_range'][0]
        for r in changed:
            if r['candle_ts']>=cutoff:
                r['exit_reason']='STOP_LOSS' if r['side']=='LONG' else 'TAKE_PROFIT'
                r['pnl_r']=-1 if r['side']=='LONG' else 3
        second=build_model(changed,999999,'SOL',15)
        self.assertEqual(first['coefficients'],second['coefficients'])
        self.assertEqual(first['intercept'],second['intercept'])
        self.assertFalse(second['ready'])


if __name__=='__main__':unittest.main()
