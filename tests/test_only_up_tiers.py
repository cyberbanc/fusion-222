from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from app import live_execution as le, worker
from app.stake_policy import stake_quote


def wei(bnb):
    return int(Decimal(str(bnb)) * 10**18)


@pytest.mark.parametrize('bank,stake', [('0',22),('999.999999',22),('1000',44),('1999.999999',44),('2000',88),('5000',88)])
def test_thresholds_exact(bank, stake):
    q=stake_quote(wei(bank),1)
    assert q['stake_usd']==stake
    assert q['value_wei']==wei(stake)


def test_tiers_decrease_without_memory():
    assert [stake_quote(wei(x),1)['stake_usd'] for x in [2100,1999,999,1001,2000]]==[88,44,22,44,88]


@pytest.mark.parametrize('price',[0,-1,'NaN','Infinity'])
def test_bad_oracle_price(price):
    with pytest.raises(ValueError): stake_quote(wei(1),price)


def test_wei_rounds_down_never_above_dollar_stake():
    q=stake_quote(wei(3),'623.12345')
    assert Decimal(q['value_wei'])/10**18*Decimal('623.12345') <= q['stake_usd']


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(le,'SETTINGS',replace(le.SETTINGS,worker_enabled=True,real_execution_enabled=True))
    snap=NS(betting_epoch=123,decision_window=True,chain_timestamp=1000,oracle_updated_at=999,chainlink_price=500,betting_round=NS(total_amount_bnb=10,bull_amount_bnb=3))
    client=NS(market_snapshot=lambda:snap,_call=lambda fn:(False,1,300))
    e=le.LiveExecutor(client);e._account=NS(address='0x0000000000000000000000000000000000000001');e._configuration_error=None
    state={'betting_epoch':123,'signal':'UP','execution_requested':True,'stake':9999,'bank_before':999999,'probability_up':.6,'expected_coeff_up':3,'payout_correction_up':1}
    def update(epoch,**fields): state.update(fields);return dict(state)
    monkeypatch.setattr(le.db,'execution_lock',lambda wallet:nullcontext())
    monkeypatch.setattr(le.db,'get_decision',lambda epoch:dict(state))
    monkeypatch.setattr(le.db,'get_reference_decision',lambda epoch:{'trade_executed':True,'signal':'UP'})
    monkeypatch.setattr(le.db,'update_execution',update)
    monkeypatch.setattr(le.db,'upsert_transaction',lambda data:data)
    monkeypatch.setattr(e,'reconcile_bet',lambda decision,price:decision)
    monkeypatch.setattr(e,'_balance_wei',lambda address:wei(3))
    monkeypatch.setattr(e,'_build_sign',lambda fn,**kw:(NS(), '0xtest',1,100000,10**9))
    sends=[]
    monkeypatch.setattr(e,'_broadcast_and_wait',lambda signed,tx:sends.append(tx))
    return e,snap,state,sends


def test_actual_wallet_sizes_bet_not_paper_bank(executor):
    e,snap,state,sends=executor
    result=e.execute_bet(state,1)
    assert result['stake']==44 and result['bank_before']==1500
    assert result['stake_bnb']==.088
    assert sends==['0xtest']


def test_down_is_blocked_at_executor(executor):
    e,snap,state,sends=executor;state['signal']='DOWN'
    assert e.execute_bet(state,500)['execution_status']=='FAILED'
    assert not sends


def test_disarmed_blocks_before_signing(executor,monkeypatch):
    e,snap,state,sends=executor
    monkeypatch.setattr(le,'SETTINGS',replace(le.SETTINGS,worker_enabled=False))
    monkeypatch.setattr(e,'_build_sign',lambda *a,**k:pytest.fail('signed while disarmed'))
    assert e.execute_bet(state,500)['execution_status']=='BLOCKED'
    assert not sends


def test_stale_price_blocks(executor):
    e,snap,state,sends=executor;snap.oracle_updated_at=900
    assert 'Stale' in e.execute_bet(state,500)['execution_error']
    assert not sends


def test_insufficient_gas_reserve_skips_without_smaller_stake(executor,monkeypatch):
    e,snap,state,sends=executor
    monkeypatch.setattr(e,'_balance_wei',lambda address:wei('.045'))
    result=e.execute_bet(state,500)
    assert result['execution_status']=='BLOCKED' and result['stake']==22
    assert not sends


def test_tier_change_between_checks_blocks(executor,monkeypatch):
    e,snap,state,sends=executor
    balances=iter([wei(3),wei(1)])
    monkeypatch.setattr(e,'_balance_wei',lambda address:next(balances))
    assert 'changed' in e.execute_bet(state,500)['execution_error']
    assert not sends


def test_existing_hash_never_resigns_or_broadcasts(executor,monkeypatch):
    e,snap,state,sends=executor;state['tx_hash']='0xknown'
    monkeypatch.setattr(e,'_build_sign',lambda *a,**k:pytest.fail('duplicate signing'))
    assert e.execute_bet(state,500)['tx_hash']=='0xknown'
    assert not sends


def test_forged_or_missing_reference_blocks(executor,monkeypatch):
    e,snap,state,sends=executor
    monkeypatch.setattr(le.db,'get_reference_decision',lambda epoch:None)
    assert 'No eligible ONLY UP' in e.execute_bet(state,500)['execution_error']
    assert not sends


def test_real_path_cannot_bypass_reference_mirror(monkeypatch):
    monkeypatch.setattr(worker,'create_mirrored_decision',lambda snap:{'sentinel':'mirror'})
    assert worker.create_locked_decision(NS())=={'sentinel':'mirror'}


def test_up_reference_is_not_sized_from_paper_wallet(monkeypatch):
    monkeypatch.setattr(worker.db,'get_decision',lambda epoch:None)
    monkeypatch.setattr(worker.db,'get_reference_decision',lambda epoch:{'signal':'UP','trade_executed':True,'bank_before':30000,'stake':888})
    monkeypatch.setattr(worker.db,'insert_decision',lambda d:d)
    d=worker.create_mirrored_decision(NS(betting_epoch=123,live_epoch=122,chain_timestamp=1000,seconds_to_lock=20,chainlink_price=500,to_dict=lambda:{}))
    assert d['stake']==0 and d['bank_before'] is None and d['execution_requested']


def test_claim_uses_contract_event_not_deposit_balance_change(executor,monkeypatch):
    e,snap,state,sends=executor
    monkeypatch.setattr(le,'SETTINGS',replace(le.SETTINGS,auto_claim_enabled=True))
    state.update(trade_executed=True,settled=True,outcome='WIN',stake_bnb=.1,bet_gas_fee_bnb=.0001,bnb_usd_price=500,claim_tx_hash='0xclaim')
    receipt={'status':1,'gasUsed':100000,'effectiveGasPrice':10**9}
    monkeypatch.setattr(e,'_receipt',lambda tx:receipt)
    monkeypatch.setattr(e,'wallet_snapshot',lambda *a,**kw:{'balance_bnb':10000})
    monkeypatch.setattr(le.db,'get_transaction',lambda tx:{'balance_before_bnb':1})
    event={'address':le.SETTINGS.prediction_contract,'args':{'sender':e.address,'epoch':123,'amount':wei('.2')}}
    e.client._call=lambda fn:[event]
    result=e.finalize_actual(state,500)
    assert result['actual_payout_bnb']==.2
    assert result['actual_pnl_bnb']==pytest.approx(.0998)
    assert result['reconciliation_status']=='CLAIM_CONFIRMED'
    assert not sends


def test_missing_claim_event_never_invents_payout(executor,monkeypatch):
    e,snap,state,sends=executor
    monkeypatch.setattr(le,'SETTINGS',replace(le.SETTINGS,auto_claim_enabled=True))
    state.update(trade_executed=True,settled=True,outcome='WIN',stake_bnb=.1,claim_tx_hash='0xclaim')
    monkeypatch.setattr(e,'_receipt',lambda tx:{'status':1})
    monkeypatch.setattr(e,'wallet_snapshot',lambda *a,**kw:{'balance_bnb':9999})
    monkeypatch.setattr(le.db,'get_transaction',lambda tx:{})
    e.client._call=lambda fn:[]
    result=e.finalize_actual(state,500)
    assert result['reconciliation_status']=='CLAIM_LOG_MISSING'
    assert result.get('actual_pnl_bnb') is None


def test_pre_incident_wallet_is_retired_even_with_live_flags(monkeypatch):
    settings=replace(le.SETTINGS,worker_enabled=True,real_execution_enabled=True,wallet_private_key='test-placeholder-not-a-real-key')
    monkeypatch.setattr(le,'SETTINGS',settings)
    monkeypatch.setattr(le.Account,'from_key',lambda key:NS(address=settings.retired_wallet_addresses[0]))
    e=le.LiveExecutor(NS())
    assert not e.ready
    assert 'must be replaced' in e.status()['configuration_error']


def test_new_wallet_can_pass_config_when_owner_supplies_it(monkeypatch):
    address='0x0000000000000000000000000000000000000001'
    monkeypatch.setattr(le,'SETTINGS',replace(le.SETTINGS,worker_enabled=True,real_execution_enabled=True,wallet_private_key='test-placeholder-not-a-real-key',expected_wallet_address=address))
    monkeypatch.setattr(le.Account,'from_key',lambda key:NS(address=address))
    assert le.LiveExecutor(NS()).ready
