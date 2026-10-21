import copy
import unittest
from unittest.mock import patch
from r3_bot.config import CONFIG as C
from r3_bot.storage import Store
from r3_bot.engine import Engine
from r3_bot.cadence import entry_limit,execution_features
from r3_bot.strategy import make_position,close_position
from r3_bot.server import summary

class CadenceTests(unittest.TestCase):
    def market(self,now=1800000010):
        s={'symbol':'SOL','instrument_id':'SOL-USDT-SWAP','ts':now,
           'instrument':{'ctVal':'1','lotSz':'.001','minSz':'.001'},
           'book':{'asks':[['100.01','100000']], 'bids':[['99.99','100000']]},
           'ticker':{'last':'100','bidPx':'99.99','askPx':'100.01'},
           'funding':{'fundingTime':'1800028800000'},'funding_history':[],
           'bars1':[],'bars_by_timeframe':{},'trades':[]}
        f={'atr':.8,'score':.2,'candle_ts':now//3600*3600-3600,
           'stop_fraction':.008,'spread_fraction':.0002,
           'source_timestamps':dict.fromkeys(['ticker','book','oi','funding','ratio'],now)}
        if C.get('scoring_profiles'):
            b={'blocks':dict.fromkeys(['structure','trend','momentum','vwap','flow','higher'],1.),
               'regime':{'state':'UP','direction':'LONG','adx':30}}
            f.update(trend=b['regime'],ballot_input=b)
        return s,f

    def test_hourly_context_can_recheck_five_minutes_later_without_overwriting(self):
        store=Store(':memory:');e=Engine(store,None);s,f=self.market()
        e.decide(s,f,60);first=store.count('decision')
        e.decide(s,f,60);self.assertEqual(store.count('decision'),first)
        s['ts']+=300;f['source_timestamps']=dict.fromkeys(f['source_timestamps'],s['ts'])
        e.decide(s,f,60)
        self.assertEqual(store.count('decision'),first*2)
        self.assertEqual({d['context_candle_ts'] for d in store.rows('decision')},{f['candle_ts']})
        self.assertEqual(len({d['decision_bucket_ts'] for d in store.rows('decision')}),2)
        self.assertTrue(any(d['reason']=='POSITION_ALREADY_OPEN' for d in store.rows('decision')))

    def test_closed_position_cannot_trigger_immediate_reentry_or_bypass_hour_cap(self):
        store=Store(':memory:');Engine(store,None);account=C['accounts'][0];now=1800000000
        for i,age in enumerate([1900,100]):
            store.put('position',str(i),{'id':str(i),'account_id':account,'status':'CLOSED','entry_ts':now-age})
        self.assertEqual(entry_limit(store,account,now),'HOURLY_ENTRY_LIMIT')
        self.assertEqual(entry_limit(store,account,now+1800),None)
        # At an exact 60-minute boundary the old entry is outside the rolling window.
        self.assertIsNone(entry_limit(store,account,now+3600))

    def test_low_volatility_uses_cost_floor_and_preserves_net_cash_goals(self):
        s,f=self.market();f['atr']=.001
        f=execution_features(s,f)
        self.assertLessEqual(f['execution_cost_fraction_of_stop'],.60000001)
        for side in ['LONG','SHORT']:
            p=make_position(s,f,side,None,500)
            self.assertTrue(p['synthetic_unfunded']);self.assertIsNone(p['margin_usd'])
            self.assertEqual(p['expires_ts']-p['entry_ts'],1800)
            self.assertAlmostEqual(close_position(p,s['ts']+60,p['take_price'],'TAKE_PROFIT')['pnl'],22,places=8)
            self.assertAlmostEqual(close_position(p,s['ts']+60,p['stop_price'],'STOP_LOSS')['pnl'],-22/3,places=8)
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_RISK_BUDGET'):
            make_position(s,f,'LONG',None,7)

    def test_stale_market_still_blocks_every_new_entry(self):
        store=Store(':memory:');e=Engine(store,None);s,f=self.market()
        f['source_timestamps']['book']-=100;e.decide(s,f,60)
        self.assertEqual(store.count('position'),0)
        self.assertTrue(all(d['reason'] in ['STALE_MARKET_DATA','LONG_SKIPPED_SHORT_ONLY'] for d in store.rows('decision')))

    def test_old_history_bank_and_entry_horizon_survive_activation(self):
        store=Store(':memory:');Engine(store,None);account=C['accounts'][0]
        a=store.get('account',account);a.update(bank=487.25,equity=487.25);store.put('account',account,a)
        store.put('position','legacy',{'id':'legacy','account_id':account,'status':'CLOSED',
            'entry_ts':1,'expires_ts':14401,'version':'legacy','pnl':-12.75})
        Engine(store,None);self.assertEqual(store.get('account',account)['bank'],487.25)
        self.assertEqual(store.get('position','legacy')['expires_ts'],14401)
        a=next(a for a in summary(store)['accounts'] if a['account_id']==account)
        self.assertEqual(a['cadence']['entries_since_policy'],0)
        self.assertEqual(a['cadence']['closed_pnl'],0)

if __name__=='__main__':unittest.main()
