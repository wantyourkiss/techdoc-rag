"""Expanded regressions run the real generator contract with explicit model doubles."""
import pytest
from scripts.probe_factual_errors import POLICIES,probe
from scripts.evaluate_regression import BOUNDARY_CASES as CASES

@pytest.mark.parametrize('policy',POLICIES)
@pytest.mark.parametrize('case',CASES,ids=lambda case:case['id'])
def test_boundary_pipeline(policy,case):
    result=probe(case,policy)
    assert result['actual_provider_calls']==0
    assert result['published']==(case['kind']=='control')
    if case['kind']=='blocked':assert result['status']=='ABSTAIN_FACT_CONTRADICTION'
