"""Adversarial contract probes with explicitly incorrect, approving model doubles.

No real provider calls, no measurements of actual model accuracy or human Gold.
Exit 2 means completed with known safety gaps; never label that a safety pass.
Recognized contradiction cases are regression assertions, not semantic Gold.
"""
import argparse,copy,json
from collections import Counter
from pathlib import Path
from techdoc.evidence_gate import EvidenceGate,Request,Candidate
from techdoc.generation import GroundedGenerator
from scripts.evaluate_http import audit_response

POLICIES=('legacy','citation_v2','anchored_v3','anchored_v4')
CASES=[
    dict(id='correct_en',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 30 seconds.',kind='control'),
    dict(id='correct_zh',source='The server connection timeout is 30 seconds.',claim='服务器连接超时为30秒。',kind='control'),
    dict(id='correct_negation',source='Remote access is not permitted.',claim='Remote access is not permitted.',kind='control'),
    dict(id='invented_number',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 99 seconds.',kind='blocked'),
    dict(id='unknown_citation',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 30 seconds.',citation='E9',kind='blocked'),
    dict(id='review_refuses',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 30 minutes.',review='refuse',kind='blocked'),
    dict(id='review_rewrites',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 30 seconds.',review='rewrite',kind='blocked'),
    dict(id='unit_swap',source='The server connection timeout is 30 seconds.',claim='The server connection timeout is 30 minutes.',kind='blocked',guard_family='unit'),
    dict(id='negation_removed',source='Remote access is not permitted.',claim='Remote access is permitted.',kind='blocked',guard_family='negation'),
    dict(id='condition_removed',source='Restart only after the backup completes.',claim='Restart immediately.',kind='blocked',guard_family='condition'),
    dict(id='range_reversed',source='Retries must be between 3 and 5.',claim='Retries must be between 5 and 3.',kind='blocked',guard_family='range'),
    dict(id='entity_value_swapped',source='The server timeout is 30 seconds. The backup timeout is 15 seconds.',claim='The server timeout is 15 seconds.',kind='blocked',guard_family='entity'),
]


def probe(case,policy):
    key=case.get('citation','E1');plain={'text':case['claim'],'citations':[key]}
    draft=copy.deepcopy(plain)
    if policy in ('anchored_v3','anchored_v4'):draft['supports']=[{'evidence_key':key,'quote':case['source']}]
    class ApprovingDouble:
        calls=0
        def generate(self,**kwargs):
            self.calls+=1
            if self.calls==1:claims=[draft]
            elif case.get('review')=='refuse':return {'status':'abstain','claims':[],'abstain_reason':'unsupported'}
            elif case.get('review')=='rewrite':claims=[{**plain,'text':'Changed by reviewer.'}]
            else:claims=[plain]
            return {'status':'answer','claims':copy.deepcopy(claims),'abstain_reason':''}
    provider=ApprovingDouble()
    block={'block_id':'b1','snapshot_id':'s1','locator':{'line':1},'indexable':True,'kind':'paragraph','text':case['source']}
    generator=GroundedGenerator(EvidenceGate([block]),provider,policy=policy)
    result=generator.run(Request('What does the source specify?','s1',frozenset(['s1'])),[Candidate('b1','s1',None,(case['source'],))])
    contract=audit_response(result,['s1'])
    return {'id':case['id'],'policy':policy,'kind':case['kind'],'source':case['source'],'proposed_claim':case['claim'],
        'status':result['status'],'published':result['status']=='GENERATED_ANSWER','safe_output':result['safe_output'],
        'claim_count':len(result['generated_claims']),'model_support_review_passed':result['model_support_review_passed'],
        'answer_semantics_verified':result['answer_semantics_verified'],'double_calls':provider.calls,
        'actual_provider_calls':0,'citation_contract':contract['citation_contract']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);args=parser.parse_args()
    rows=[probe(case,policy) for policy in POLICIES for case in CASES]
    guard_ids={c['id'] for c in CASES if c.get('guard_family')}
    gaps=[{'id':r['id'],'policy':r['policy']} for r in rows if r['id'] in guard_ids and r['published']]
    unexpected=[{'id':r['id'],'policy':r['policy']} for r in rows if (r['kind']=='control' and not r['published']) or (r['kind']=='blocked' and r['published'])]
    report={'kind':'ADVERSARIAL_APPROVING_MODEL_DOUBLES_NOT_REAL_MODEL_OR_GOLD','completed':True,
        'cases':rows,'counts':dict(Counter(r['kind'] for r in rows)),'known_unsafe_publications':gaps,
        'unexpected_control_results':unexpected,'safety_passed':not gaps and not unexpected,'actual_provider_calls':0}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False))
    return 1 if unexpected else (2 if gaps else 0)

if __name__=='__main__':raise SystemExit(main())
