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

    def test_24_independent_modes(self):
        store=Store(':memory:');Engine(store,None)
        accounts=store.rows('account',limit=None)
        self.assertEqual(len(accounts),24)
        self.assertEqual({a['direction_mode'] for a in accounts},{'LONG','BOTH','SHORT'})
        self.assertEqual({a['timeframe_minutes'] for a in accounts},{5,15,30,60})
        self.assertTrue(all(a['bank']==500 for a in accounts))

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


if __name__=='__main__':unittest.main()
