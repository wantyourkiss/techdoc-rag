"""Runner must notice an unsafe release, not merely format a success report."""
from scripts import evaluate_regression as suite

def test_runner_baseline_and_injected_guard_regression(monkeypatch):
    result=suite.evaluate()
    assert result['passed'] and result['actual_provider_calls']==0
    assert result['metrics']['answer_cases']==80
    assert result['metrics']['real_generation_accuracy'] is None
    monkeypatch.setattr('techdoc.generation.contradiction',lambda *_:None)
    broken=suite.evaluate()
    assert not broken['passed'] and broken['metrics']['unsafe_publications']>0
