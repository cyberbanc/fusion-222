"""USD stake tiers, measured against liquid native BNB before the bet.

Locked bets, unclaimed rewards, tokens and the paper bank are excluded.
Gas reserve is affordability protection, not a deduction from tier thresholds.
"""
from decimal import Decimal, ROUND_DOWN

WEI = Decimal(10**18)
POLICY = {"mode": "bnb_usd_tiers_22_44_88", "bank_source": "liquid_native_bnb", "boundaries": [1000, 2000], "stakes": [22, 44, 88], "boundary_rule": "1000<=bank<2000:44; bank>=2000:88", "maximum_stake_usd": 88}


def stake_quote(balance_wei: int, price: float | str | Decimal) -> dict:
    price = Decimal(str(price))
    if balance_wei < 0 or not price.is_finite() or price <= 0:
        raise ValueError("Invalid native BNB balance or BNB/USD price")
    bank = Decimal(balance_wei) / WEI * price
    stake = Decimal(88 if bank >= 2000 else 44 if bank >= 1000 else 22)
    value = int((stake / price * WEI).to_integral_value(rounding=ROUND_DOWN))
    return {"bank_usd": float(bank), "stake_usd": float(stake), "stake_tier": f"USD_{stake}", "value_wei": value, "stake_bnb": float(Decimal(value) / WEI), "bnb_usd_price": float(price)}
