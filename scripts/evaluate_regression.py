"""Reproducible synthetic retrieval/answer-contract regressions, NOT human Gold.

No service credentials, external providers, dense-model substitutes or production
data. Answer generation/review use explicitly approving doubles; retrieval uses
the real BM25 implementation. Exit nonzero for any violated expected behavior.
"""
import argparse,hashlib,json,platform,time
from pathlib import Path
import techdoc
from techdoc.core import LexicalIndex
from techdoc.evidence_gate import Candidate,EvidenceGate,Request
from techdoc.generation import GroundedGenerator

POLICIES=('legacy','citation_v2','anchored_v3','anchored_v4')
BOUNDARY_CASES=[
 dict(id='local_contrast',source='Local access is permitted, but remote access is not permitted.',claim='Local access is permitted.',kind='control'),
 dict(id='local_contrast_zh',source='本地访问允许，但远程访问不允许。',claim='本地访问允许。',kind='control'),
 dict(id='supported_alternative',source='Restart only after the backup or the test completes.',claim='Restart only after the backup or the test completes.',kind='control'),
 dict(id='weakened_condition',source='Restart only after the backup completes.',claim='Restart only after the backup completes, or immediately if needed.',kind='blocked'),
 dict(id='weakened_condition_zh',source='只有备份完成后才允许重启。',claim='只有备份完成后才允许重启，或者需要时立即重启。',kind='blocked'),
 dict(id='discourse_exception',source='Restart only after the backup completes.',claim='Restart only after the backup completes. Alternatively, restart immediately.',kind='blocked'),
 dict(id='remote_contrast',source='Local access is permitted, but remote access is not permitted.',claim='Remote access is permitted.',kind='blocked'),
 dict(id='remote_contrast_zh',source='本地访问允许，但远程访问不允许。',claim='远程访问允许。',kind='blocked'),
]

def answer_case(case,policy):
    class ApprovingDouble:
        def generate(self,**kwargs):
            claim={'text':case['claim'],'citations':['E1']}
            if 'output_schema' in kwargs:claim['supports']=[{'evidence_key':'E1','quote':case['source']}]
            return {'status':'answer','claims':[claim],'abstain_reason':''}
    block={'block_id':'b','snapshot_id':'s','text':case['source'],'indexable':True,'kind':'paragraph','locator':{'line':1}}
    result=GroundedGenerator(EvidenceGate([block]),ApprovingDouble(),policy=policy).run(
        Request('What does the source specify?','s',frozenset(['s'])),[Candidate('b','s',None,(case['source'],))])
    published=result['status']=='GENERATED_ANSWER';expected=case['kind']=='control'
    return {'id':case['id'],'policy':policy,'expected_published':expected,'published':published,
      'status':result['status'],'passed':published==expected,'actual_provider_calls':0,
      'source_sha256':hashlib.sha256(case['source'].encode()).hexdigest()}

def retrieval_cases():
    chunks=[{'chunk_id':'alpha','snapshot_id':'v1','index_text':'Orion server connection timeout is 30 seconds.'},
            {'chunk_id':'beta','snapshot_id':'v2','index_text':'Pegasus backup retry limit is 5 attempts.'},
            {'chunk_id':'old','snapshot_id':'v0','index_text':'Orion server connection timeout is 99 seconds.'}]
    index=LexicalIndex(chunks);rows=[]
    for ident,question,scope,wanted in [('timeout','Orion server connection timeout',{'v1','v2'},'alpha'),
      ('backup','Pegasus backup retry limit',{'v1','v2'},'beta'),
      ('historical_version','Orion server connection timeout',{'v0'},'old')]:
        hits=index.query(question,frozenset(scope),topk=3);ids=[h['chunk']['chunk_id'] for h in hits]
        rank=ids.index(wanted)+1 if wanted in ids else None
        scope_ok=all(h['chunk']['snapshot_id'] in scope for h in hits)
        rows.append({'id':ident,'expected_chunk':wanted,'ranking':ids,'reciprocal_rank':1/rank if rank else 0,
                     'expected_in_top3':rank is not None,'scope_preserved':scope_ok,'passed':rank==1 and scope_ok})
    return rows

def evaluate():
    # Importing the existing probe here reuses the original baseline cases while
    # keeping the standalone boundary suite usable in the minimal public repo.
    from scripts.probe_factual_errors import CASES,probe
    start=time.monotonic();baseline=[]
    for case in CASES:
        for policy in POLICIES:
            result=probe(case,policy);expected=case['kind']=='control'
            baseline.append({'id':case['id'],'policy':policy,'expected_published':expected,'published':result['published'],
              'passed':result['published']==expected,'status':result['status'],'actual_provider_calls':0})
    answers=baseline+[answer_case(case,policy) for case in BOUNDARY_CASES for policy in POLICIES]
    retrieval=retrieval_cases();root=Path(techdoc.__file__).resolve().parent.parent
    files=sorted(list((root/'techdoc').rglob('*.py'))+[root/'scripts/probe_factual_errors.py'])
    manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files if '__pycache__' not in p.parts}
    metrics={'answer_cases':len(answers),'unsafe_publications':sum(not r['expected_published'] and r['published'] for r in answers),
      'false_refusals':sum(r['expected_published'] and not r['published'] for r in answers),
      'synthetic_bm25_queries':len(retrieval),'synthetic_top1_hits':sum(r['passed'] for r in retrieval),
      'synthetic_mrr':sum(r['reciprocal_rank'] for r in retrieval)/len(retrieval),
      'real_dense_quality':None,'real_generation_accuracy':None,'human_gold':False}
    return {'contract':'synthetic_regression_v1','kind':'AUTOMATED_CONTRACT_REGRESSION_NOT_REAL_MODEL_OR_GOLD',
      'passed':all(r['passed'] for r in answers+retrieval),'actual_provider_calls':0,'metrics':metrics,
      'elapsed_seconds':round(time.monotonic()-start,3),'python':platform.python_version(),
      'policy_ids':list(POLICIES),'source_manifest':manifest,
      'recipe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'answers':answers,'retrieval':retrieval}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True);args=parser.parse_args()
    result=evaluate();out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    metrics=result['metrics'];lines=['# Synthetic contract regression','',
      'Approving model doubles and synthetic BM25 facts. Not real-model accuracy or human Gold.','',
      f"Passed: {result['passed']}",f"Answer cases: {metrics['answer_cases']}",
      f"Unsafe publications: {metrics['unsafe_publications']}",f"False refusals: {metrics['false_refusals']}",
      f"Synthetic BM25 top-1: {metrics['synthetic_top1_hits']}/{metrics['synthetic_bm25_queries']}"]
    (out/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'metrics':metrics},ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
