import copy
import io
import json
import unittest
from unittest.mock import Mock,patch
import zipfile
from r3_bot.storage import Store
from r3_bot.config import CONFIG as C
from r3_bot.engine import Engine,advance
from r3_bot.strategy import make_position,close_position,calibrate,price_for_net_r,net_at_price
from r3_bot.server import history_archive
from r3_bot.trend import trend_state,classify_regime,directional_series


def snapshot():
    return {'symbol':'SOL','instrument_id':'SOL-USDT-SWAP','ts':1800000010,
        'instrument':{'ctVal':'1','lotSz':'.001','minSz':'.001'},
        'book':{'asks':[['100.01','100000']], 'bids':[['99.99','100000']]},
        'ticker':{'last':'100','bidPx':'99.99','askPx':'100.01'},
        'funding':{'fundingTime':'1800028800000'},'funding_history':[],'bars1':[]}


class TradingTests(unittest.TestCase):
    def test_worker_resumes_after_rolling_deployment_lock_handoff(self):
        store=Store(':memory:');engine=Engine(store,None)
        store.acquire_worker=Mock(side_effect=[False,False,True])
        engine.stop.wait=Mock(return_value=False)
        def once():
            self.assertGreaterEqual(store.acquire_worker.call_count,3)
            engine.stop.set()
        engine.once=Mock(side_effect=once)
        engine.run()
        engine.once.assert_called_once()

    def test_24_independent_classic_modes(self):
        store=Store(':memory:');Engine(store,None)
        accounts=store.rows('account',limit=None)
        self.assertEqual(len(accounts),24)
        self.assertEqual({a['direction_mode'] for a in accounts},{'LONG','BOTH','SHORT'})
        self.assertEqual(sum(a['direction_mode']=='TREND' for a in accounts),0)
        self.assertEqual({a['timeframe_minutes'] for a in accounts},{5,15,30,60})
        self.assertTrue(all(a['bank']==500 for a in accounts))

    def test_retired_trend_accounts_hidden_but_history_preserved(self):
        from r3_bot.server import summary
        store=Store(':memory:');Engine(store,None)
        base=store.get('account','SOL_15_LONG')
        for symbol in ['SOL','ETH']:
            for tf in [5,15,30,60]:
                key=f'{symbol}_{tf}_TREND';a=copy.deepcopy(base)
                a.update(account_id=key,symbol=symbol,timeframe_minutes=tf,direction_mode='TREND',bank=487.25,equity=487.25)
                store.put('account',key,a)
                store.put('decision',key+':old',{'account_id':key,'created_ts':123,'executed':False})
        Engine(store,None)
        result=summary(store)
        self.assertEqual(len(result['accounts']),24)
        self.assertEqual(result['archived_account_count'],8)
        for a in store.rows('account',limit=None):
            if a['direction_mode']=='TREND':
                self.assertFalse(a['active']);self.assertEqual(a['status'],'ARCHIVED')
                self.assertEqual(a['bank'],487.25)
        archive,counts=history_archive(store)
        self.assertEqual(counts['account'],32);self.assertEqual(counts['decision'],8)
        archive.close()

    def test_net_three_to_one_in_both_directions(self):
        s=snapshot()
        for side in ['LONG','SHORT']:
            p=make_position(s,{'atr':.8,'score':.2},side,5,500)
            loss=close_position(p,p['entry_ts']+60,p['stop_price'],'STOP_LOSS')
            win=close_position(p,p['entry_ts']+60,p['take_price'],'TAKE_PROFIT')
            self.assertAlmostEqual(loss['pnl_r'],-1,places=10)
            self.assertAlmostEqual(win['pnl_r'],3,places=10)
            self.assertAlmostEqual(loss['pnl'],-22/3,places=9)
            self.assertAlmostEqual(win['pnl'],22,places=9)
            self.assertLessEqual(p['margin_usd'],450.001)

    def test_cash_target_independent_of_bank_and_atr(self):
        s=snapshot()
        notionals=[]
        for side in ['LONG','SHORT']:
            for bank in [500,1000,2000]:
                for atr in [.8,1.2]:
                    p=make_position(s,{'atr':atr,'score':.2},side,None,bank)
                    self.assertEqual(p['position_sizing'],'FIXED_CASH_TARGET')
                    self.assertAlmostEqual(p['risk_usd'],22/3,places=10)
                    self.assertEqual(p['planned_reward_usd'],22)
                    self.assertAlmostEqual(net_at_price(p,p['stop_price']),-1,places=10)
                    self.assertAlmostEqual(net_at_price(p,p['take_price']),3,places=10)
                    notionals.append(p['notional_usd'])
        self.assertGreater(max(notionals),min(notionals)*1.3)

    def test_cash_target_never_silently_reduced_to_fit_margin(self):
        s=snapshot();s['instrument'].update(lotSz='100',minSz='100')
        with self.assertRaisesRegex(ValueError,'MIN_CONTRACT_SIZE'):
            make_position(s,{'atr':.8,'score':.2},'LONG',None,500)
        with self.assertRaisesRegex(ValueError,'CASH_TARGET_LEVERAGE_LIMIT'):
            make_position(snapshot(),{'atr':.05,'score':.2},'LONG',None,500)
        with self.assertRaisesRegex(ValueError,'CASH_TARGET_LEVERAGE_LIMIT'):
            make_position(snapshot(),{'atr':.8,'score':.2},'SHORT',None,5)

    def test_legacy_open_position_keeps_its_original_target(self):
        s=snapshot()
        p=make_position(s,{'atr':.8,'score':.2},'LONG',.2,500,trial=True)
        p.update(trial=False,version='FUSION-R3-v1.1.0',position_sizing='FIXED_NOTIONAL')
        s['ts']+=60
        s['ticker'].update(bidPx=str(p['take_price']),askPx=str(p['take_price']+.01))
        result=advance(p,s)
        self.assertEqual(result['exit_reason'],'TAKE_PROFIT')
        self.assertEqual(result['version'],'FUSION-R3-v1.1.0')
        self.assertAlmostEqual(result['pnl'],.6,places=10)

    def test_allowed_signal_creates_cash_positions_and_exit_studies(self):
        store=Store(':memory:');engine=Engine(store,None);s=snapshot()
        s.update(trades=[],bars_by_timeframe={})
        f={'atr':.8,'score':.2,'candle_ts':s['ts']//900*900-900,
            'spread_fraction':.0002,'stop_fraction':.012,
            'source_timestamps':dict.fromkeys(['ticker','book','oi','funding','ratio'],s['ts'])}
        estimate={'ready':True,'probability':.7,'ev_r':1,'samples':15}
        with patch('r3_bot.engine.calibrate',return_value=estimate):engine.decide(s,f,15)
        positions=store.rows('position',limit=None)
        self.assertEqual({p['account_id'] for p in positions},{'SOL_15_LONG','SOL_15_BOTH'})
        for p in positions:
            self.assertEqual(p['planned_reward_usd'],22)
            self.assertAlmostEqual(p['risk_usd'],22/3)
        self.assertEqual(store.count('study'),2)
        for study in store.rows('study'):
            self.assertEqual(len(study['cases']),16)
            for case in study['cases'].values():
                self.assertAlmostEqual(case['planned_reward_usd'],case['study_tp_r']*case['risk_usd'])

    def test_shadow_risk_remains_normalized(self):
        p=make_position(snapshot(),{'atr':.8,'score':.2},'SHORT',1,500,trial=True)
        self.assertEqual(p['position_sizing'],'SHADOW_FIXED_RISK')
        self.assertAlmostEqual(p['risk_usd'],1,places=10)

    def test_no_old_direction_probability_substituted(self):
        result=calibrate([], 'LONG', .9)
        self.assertFalse(result['ready'])
        self.assertEqual(result['probability'],.25)

    def test_calibration_requires_15_closed_trials_per_direction(self):
        rows=[{'side':'LONG','exit_reason':'TAKE_PROFIT','score':.2,
            'pnl_r':3,'exit_ts':100+i} for i in range(15)]
        self.assertFalse(calibrate(rows[:14],'LONG',.2)['ready'])
        self.assertTrue(calibrate(rows,'LONG',.2)['ready'])
        self.assertFalse(calibrate(rows,'SHORT',.2)['ready'])

    def test_entry_partial_minute_not_replayed_with_past_extremes(self):
        s=snapshot();p=make_position(s,{'atr':.8,'score':.2},'LONG',5,500)
        s['ts']+=60
        start=p['entry_ts']//60*60
        s['bars1']=[[str(start*1000),'100','110','90','100','1','1','100','1']]
        updated=advance(p,s)
        self.assertEqual(updated['status'],'OPEN')

    def test_80_percent_reversal_and_smaller_target(self):
        s=snapshot();p=make_position(s,{'atr':.8,'score':.2},'LONG',5,500)
        smaller=copy.deepcopy(p);smaller['take_price']=price_for_net_r(p,2)
        next_minute=p['last_minute']+60
        high=p['entry_price']+.85*(p['take_price']-p['entry_price'])
        low=p['entry_price']-.05
        s['ts']=next_minute+60
        s['ticker'].update(bidPx=str(high),askPx=str(high+.01))
        s['bars1']=[[str(next_minute*1000),'100',str(high),str(low),str(high),'1','1','100','1']]
        main=advance(p,s);alt=advance(smaller,s)
        self.assertEqual(main['status'],'OPEN')
        self.assertGreaterEqual(main['tp_price_progress'],.8)
        self.assertEqual(alt['exit_reason'],'TAKE_PROFIT')
        self.assertAlmostEqual(alt['pnl_r'],2,places=8)
        next_minute+=60;s['ts']=next_minute+60
        s['bars1']=[[str(next_minute*1000),'100','100',str(p['stop_price']-.01),str(p['stop_price']),'1','1','100','1']]
        main=advance(main,s)
        self.assertEqual(main['status'],'CLOSED')
        self.assertTrue(main['near_tp_reversal'])
        self.assertTrue(main['missed_2r'])

    def test_same_minute_order_does_not_invent_take_first(self):
        s=snapshot();p=make_position(s,{'atr':.8,'score':.2},'LONG',5,500)
        minute=p['last_minute']+60;s['ts']=minute+60
        s['bars1']=[[str(minute*1000),'100',str(p['take_price']+1),str(p['stop_price']-1),'100','1','1','100','1']]
        updated=advance(p,s)
        self.assertEqual(updated['exit_reason'],'STOP_LOSS_BOTH_TOUCHED')
        self.assertTrue(updated['excursion_order_ambiguous'])
        self.assertFalse(updated['near_tp_reversal'])
        self.assertTrue(updated['near_tp_reversal_possible'])

    def test_export_has_every_record_beyond_ui_limit(self):
        store=Store(':memory:');Engine(store,None)
        for i in range(321):
            store.put('decision',str(i),{'created_ts':i,'account_id':'SOL_15_LONG','index':i})
        archive,counts=history_archive(store)
        self.assertEqual(counts['decision'],321)
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(len(z.read('decision.jsonl').splitlines()),321)
            self.assertEqual(json.loads(z.read('manifest.json'))['counts']['account'],24)
        archive.close()


def trend_snapshot(closes,tf=15):
    now=1800000000;start=now-len(closes)*tf*60
    bars=[[str((start+i*tf*60)*1000),str(c),str(c+.2),str(c-.2),str(c),'1','1','100','1']
        for i,c in enumerate(closes)]
    return {'ts':now,'bars_by_timeframe':{str(tf):bars}}


class TrendTests(unittest.TestCase):
    def setUp(self):
        # Legacy profile remains replayable in tests; production config is 24 classics.
        from r3_bot.config import CONFIG
        keys=CONFIG['accounts']+[f'{s}_{tf}_TREND' for s in ['SOL','ETH'] for tf in [5,15,30,60]]
        self.config_patch=patch.dict(CONFIG,{'accounts':keys});self.config_patch.start()

    def tearDown(self):self.config_patch.stop()
    def test_monotonic_up_down_have_dmi_direction_and_adx_strength(self):
        for sign,side,state in [(1,'LONG','UP'),(-1,'SHORT','DOWN')]:
            s=trend_snapshot([200+sign*i*.5 for i in range(100)])
            tr=trend_state(s,15)
            self.assertEqual(tr['state'],state);self.assertEqual(tr['direction'],side)
            self.assertAlmostEqual(tr['adx'],100,places=8)

    def test_flat_waits_and_breakout_uses_prior_channel(self):
        closes=[100+(.05 if i%2 else -.05) for i in range(100)]
        flat=trend_state(trend_snapshot(closes),15)
        self.assertEqual(flat['state'],'FLAT');self.assertEqual(flat['direction'],'WAIT')
        for final,side,state in [(102,'LONG','BREAKOUT_UP'),(98,'SHORT','BREAKOUT_DOWN')]:
            tr=trend_state(trend_snapshot(closes+[final]),15)
            self.assertEqual(tr['state'],state);self.assertEqual(tr['direction'],side)
            self.assertLess(tr['channel_end_ts'],tr['candle_ts'])
            self.assertLess(tr['support'],tr['resistance'])

    def test_wick_is_not_a_confirmed_breakout(self):
        s=trend_snapshot([100+(.05 if i%2 else -.05) for i in range(101)])
        s['bars_by_timeframe']['15'][-1][2]='110'
        self.assertEqual(trend_state(s,15)['direction'],'WAIT')

    def test_future_and_unconfirmed_bars_do_not_change_signal(self):
        s=trend_snapshot([100+i*.5 for i in range(100)]);before=trend_state(s,15)
        for confirm in ['0','1']:
            candidate=copy.deepcopy(s)
            candidate['bars_by_timeframe']['15'].append([str(s['ts']*1000),'10000','11000','1','10000','1','1','1',confirm])
            self.assertEqual(trend_state(candidate,15),before)

    def profile_input(self,state,side):
        s=snapshot();s.update(trades=[],bars_by_timeframe={})
        reason='TREND_FLAT_WAIT_BREAKOUT' if side=='WAIT' else 'ALLOWED'
        f={'atr':.8,'score':-.8,'candle_ts':s['ts']//900*900-900,
            'spread_fraction':.0002,'stop_fraction':.012,
            'source_timestamps':dict.fromkeys(['ticker','book','oi','funding','ratio'],s['ts']),
            'trend':{'state':state,'direction':side,'reason':reason,'score':.8 if side=='LONG' else -.8}}
        return s,f

    def test_trend_direction_cannot_be_overridden_by_opposite_ev(self):
        for state,side in [('UP','LONG'),('DOWN','SHORT')]:
            store=Store(':memory:');engine=Engine(store,None);s,f=self.profile_input(state,side)
            def estimate(rows,direction,score):
                return {'ready':True,'probability':.7,'ev_r':.1 if direction==side else 1.5,'samples':15}
            with patch('r3_bot.engine.calibrate',side_effect=estimate):
                engine.decide(s,f,15,profile='TREND');engine.decide(s,f,15,profile='TREND')
            positions=store.rows('position',limit=None)
            self.assertEqual(len(positions),1)
            self.assertEqual(positions[0]['account_id'],'SOL_15_TREND')
            self.assertEqual(positions[0]['side'],side)
            self.assertEqual(positions[0]['planned_reward_usd'],22)
            self.assertAlmostEqual(positions[0]['risk_usd'],22/3)
            self.assertEqual(store.count('decision'),1)
            self.assertEqual(store.count('trial'),2)

    def test_flat_blocks_entries_even_with_good_probability(self):
        store=Store(':memory:');engine=Engine(store,None);s,f=self.profile_input('FLAT','WAIT')
        with patch('r3_bot.engine.calibrate',return_value={'ready':True,'probability':.9,'ev_r':2,'samples':15}):
            engine.decide(s,f,15,profile='TREND')
        self.assertEqual(store.count('position'),0)
        self.assertEqual(store.rows('decision')[0]['reason'],'TREND_FLAT_WAIT_BREAKOUT')
        self.assertTrue(all(not p['selected'] for p in store.rows('trial')))

    def test_trend_calibration_is_separate_from_classic(self):
        store=Store(':memory:');engine=Engine(store,None);s,f=self.profile_input('UP','LONG')
        for i in range(30):
            store.put('trial','classic'+str(i),{'account_id':'SOL_15_SHADOW','status':'CLOSED',
                'side':'LONG' if i%2 else 'SHORT','score':.8,'pnl_r':3,'exit_reason':'TAKE_PROFIT',
                'created_ts':i,'exit_ts':s['ts']-1,'selected':True})
        engine.decide(s,f,15,profile='TREND')
        self.assertEqual(store.count('position'),0)
        self.assertEqual(store.rows('decision')[0]['calibration_samples'],0)
        self.assertEqual(store.rows('decision')[0]['reason'],'CALIBRATION_WARMUP')


if __name__=='__main__':unittest.main()
