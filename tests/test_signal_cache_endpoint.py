from app import main


def test_signal_wait_uses_live_cache(monkeypatch):
    monkeypatch.setattr(main,"signal_cache",lambda:{"ok":True,"snapshot":{"betting_epoch":456,"live_epoch":455,"seconds_to_lock":91},"decision":None,"worker_tick":{"betting_epoch":456,"live_epoch":455,"seconds_to_lock":91},"updated_at":1000})
    monkeypatch.setattr(main.db,"enabled",lambda:False)
    result=main.signal()
    assert result["status"]=="WAIT"
    assert result["betting_epoch"]==456
