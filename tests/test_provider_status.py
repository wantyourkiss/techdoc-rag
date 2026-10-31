import io,json
from urllib.error import HTTPError
from unittest.mock import patch
import pytest
from techdoc.s3b_generator import OpenAIChatClient,ProviderError
from techdoc.provider_status import public_failure,failure_message
from techdoc.evidence_gate import EvidenceGate,Request,Candidate
from techdoc.generation import GroundedGenerator


@pytest.mark.parametrize('detail,kind,attempts',[
    ({'type':'GoUsageLimitError','message':'PRIVATE_KEY_PRIVATE_PROMPT'},'quota',1),
    ({'code':'insufficient_quota'},'quota',1),
    ({'type':'RateLimitError'},'rate_limit',3),
    ({'type':['malformed'],'message':'PRIVATE_KEY_PRIVATE_PROMPT'},'rate_limit',3),
])
def test_only_recognized_quota_errors_skip_retry_and_body_never_leaks(detail,kind,attempts):
    def error(*args,**kwargs):
        raise HTTPError('https://private.invalid',429,'PRIVATE_MESSAGE',{},io.BytesIO(json.dumps({'error':detail}).encode()))
    client=OpenAIChatClient(model='test',base_url='https://example.invalid',api_key='PRIVATE_KEY',max_retries=2)
    with patch('techdoc.s3b_generator.urlopen',side_effect=error) as transport,patch('techdoc.s3b_generator.time.sleep'):
        with pytest.raises(ProviderError) as caught:client.generate(system='PRIVATE_PROMPT',user='test')
    assert transport.call_count==attempts and caught.value.failure_kind==kind
    assert 'PRIVATE' not in json.dumps(public_failure(caught.value,'generation'))
    assert str(caught.value)=='PROVIDER_HTTP_429'


@pytest.mark.parametrize('category,kind',[
 ('PROVIDER_NETWORK_OR_TIMEOUT','network'),('PROVIDER_HTTP_401','authentication'),
 ('PROVIDER_HTTP_403','access_denied'),('PROVIDER_HTTP_503','unavailable'),
 ('MALFORMED_PROVIDER_RESPONSE','invalid_response'),('PRIVATE_EXCEPTION','unknown'),
])
@pytest.mark.parametrize('failure_stage',['generation','support_review'])
def test_generation_failure_has_safe_stage_and_no_generated_answer(category,kind,failure_stage):
    class Provider:
        calls=0
        def generate(self,**kwargs):
            self.calls+=1
            if failure_stage=='generation' or self.calls==2:raise ProviderError(category)
            return {'status':'answer','claims':[{'text':'Timeout is 30 seconds.','citations':['E1']}],'abstain_reason':''}
    block={'block_id':'b','snapshot_id':'s','indexable':True,'kind':'paragraph','text':'Timeout is 30 seconds.','locator':{'line':1}}
    result=GroundedGenerator(EvidenceGate([block]),Provider()).run(Request('What is the timeout?','s',frozenset(['s'])),[Candidate('b','s',None,('Timeout is 30 seconds.',))])
    assert result['status']=='ABSTAIN_PROVIDER_FAILURE' and not result['safe_output'] and not result['generated_claims']
    assert result['provider_failure']['kind']==kind and result['provider_failure']['stage']==failure_stage
    assert result['reason']==failure_message(result['provider_failure'])
    assert 'PRIVATE' not in json.dumps(result)


def test_unknown_exception_attributes_are_not_serialized():
    error=RuntimeError('PRIVATE_SECRET');error.failure_kind={'secret':'PRIVATE_SECRET'};error.category='PRIVATE_SECRET'
    assert public_failure(error,'PRIVATE_STAGE')=={'kind':'unknown','stage':'generation','retryable':False,'contract':'provider_status_v1'}
