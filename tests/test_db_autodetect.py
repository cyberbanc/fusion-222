from app import db


def test_choose_existing_table_prefers_legacy_candidate_when_configured_missing():
    existing = {"decisions", "round_history", "fusion222_v1366_nobreaker_decisions"}
    columns = {
        "decisions": {"betting_epoch", "signal", "selected_ev", "probability_up", "probability_down", "strategy_version", "settled", "final_winner"},
        "fusion222_v1366_nobreaker_decisions": {"betting_epoch", "signal", "selected_ev", "probability_up", "probability_down", "strategy_version", "settled", "final_winner"},
    }
    got = db._choose_existing_table(
        existing, columns, "paper_decisions",
        ("paper_decisions", "decisions", "fusion_decisions"),
        {"betting_epoch", "signal", "selected_ev", "probability_up", "probability_down", "strategy_version", "settled", "final_winner"},
        exclude={"fusion222_v1366_nobreaker_decisions"}, min_signature_matches=5,
    )
    assert got == "decisions"


def test_choose_existing_table_signature_fallback_never_selects_fusion222_private():
    existing = {"m9_history", "fusion222_v1366_nobreaker_decisions"}
    sig = {"betting_epoch", "signal", "selected_ev", "probability_up", "probability_down", "strategy_version", "settled", "final_winner"}
    columns = {
        "m9_history": set(sig),
        "fusion222_v1366_nobreaker_decisions": set(sig),
    }
    got = db._choose_existing_table(
        existing, columns, "auto", (), sig,
        exclude={"fusion222_v1366_nobreaker_decisions"}, min_signature_matches=5,
    )
    assert got == "m9_history"
