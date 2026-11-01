import pytest
from techdoc.fact_guard import contradiction
from scripts.probe_factual_errors import probe,POLICIES

@pytest.mark.parametrize('source,claim',[
 ('The connection timeout is at most 30 seconds.','The connection timeout is at least 30 seconds.'),
 ('Connection timeout is below 30 seconds.','Connection timeout is above 30 seconds.'),
 ('Connection timeout is at most 30 seconds.','Connection timeout is less than 30 seconds.'),
 ('CPU usage is <= 80%.','CPU usage is > 80%.'),
 ('连接超时最多30秒。','连接超时至少30秒。'),
 ('连接超时不得低于30秒。','连接超时不得超过30秒。'),
 ('连接超时大于等于30秒。','连接超时小于等于30秒。'),
])
def test_changed_direction_and_strictness(source,claim):
 assert contradiction(claim,source)=='THRESHOLD_CONTRADICTION'

@pytest.mark.parametrize('source,claim',[
 ('Connection timeout is at most 30 seconds.','Connection timeout is no more than 30 seconds.'),
 ('Connection timeout is at least 30 seconds.','Connection timeout is not less than 30 seconds.'),
 ('Connection timeout is less than 30 seconds.','Connection timeout is at most 30 seconds.'),
 ('Connection timeout is greater than 30 seconds.','Connection timeout is at least 30 seconds.'),
 ('连接超时最多30秒。','连接超时不超过30秒。'),
 ('连接超时小于30秒。','连接超时小于等于30秒。'),
 ('Connection timeout is at most 30 seconds. Backup duration is at least 30 seconds.','Backup duration is at least 30 seconds.'),
 ('Connection timeout is at most 30 seconds. Backup duration is at least 30 minutes.','Backup duration is at least 30 minutes.'),
])
def test_legitimate_bounds_and_other_properties(source,claim):
 assert contradiction(claim,source) is None

@pytest.mark.parametrize('policy',POLICIES)
def test_approving_models_cannot_publish_threshold_flip(policy):
 result=probe({'id':'threshold','source':'The connection timeout is at most 30 seconds.',
               'claim':'The connection timeout is at least 30 seconds.','kind':'blocked'},policy)
 assert not result['published'] and result['status']=='ABSTAIN_FACT_CONTRADICTION'
 assert not result['answer_semantics_verified'] and result['actual_provider_calls']==0
