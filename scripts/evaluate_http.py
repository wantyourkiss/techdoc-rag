"""Reproducible HTTP development evaluation, never an automatic human-Gold claim.

python -m scripts.evaluate_http --config config.json --cases cases.json --output new_dir
Browser credentials come from TECHDOC_BROWSER_USER/PASSWORD or --env-file, never reports.
"""
import argparse,base64,hashlib,json,os,ssl,time,re
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def resolve_documents(specs,documents):
    selected={}
    for key,spec in specs.items():
        matches=[d for d in documents if all(d.get(k)==v for k,v in spec.items())]
        if len(matches)!=1:raise ValueError('DOCUMENT_NOT_UNIQUE_'+key)
        selected[key]=matches[0]
    return selected


def build_manifest(root):
    root=Path(root)
    files=[p for name in ('techdoc','scripts') for p in (root/name).rglob('*')
           if p.is_file() and p.suffix in ('.py','.js','.html','.css','.json')]
    files.extend(root.glob('requirements*.txt'))
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def audit_response(response,scope,blocks=None,*,required_scope=None):
    required=set(scope) if required_scope is None else set(required_scope)
    if not required<=set(scope):raise ValueError('BAD_REQUIRED_SCOPE')
    status=response.get('status');claims=response.get('generated_claims',[])
    extracts=response.get('source_extracts',[])
    fallback=response.get('retrieved_evidence',[])
    if fallback:
        if (status=='GENERATED_ANSWER' or extracts or claims or response.get('safe_output')
            or response.get('model_support_review_passed')
            or response.get('retrieved_evidence_kind')!='source_excerpts_not_an_answer'):
            raise ValueError('UNSAFE_RETRIEVAL_FALLBACK')
        extracts=fallback
    lookup={e['evidence_key']:e for e in extracts}
    if len(lookup)!=len(extracts):raise ValueError('DUPLICATE_EVIDENCE_KEY')
    for e in extracts:
        if e['snapshot_id'] not in scope or not e.get('quotes') or any(not isinstance(q,str) or not q.strip() for q in e['quotes']):
            raise ValueError('EMPTY_OR_OUT_OF_SCOPE_CITATION')
    if status=='GENERATED_ANSWER':
        if not claims:raise ValueError('INCOMPLETE_CITATION_SCOPE')
        used=set()
        for claim in claims:
            if not claim['citations'] or len(set(claim['citations']))!=len(claim['citations']) or not set(claim['citations'])<=set(lookup):raise ValueError('UNKNOWN_CITATION')
            cited='\n'.join(q for k in claim['citations'] for q in lookup[k]['quotes'])
            if not set(re.findall(r'\d+(?:\.\d+)?',claim['text']))<=set(re.findall(r'\d+(?:\.\d+)?',cited)):
                raise ValueError('UNSUPPORTED_NUMBER')
            used.update(claim['citations'])
        if not required<={lookup[k]['snapshot_id'] for k in used}:raise ValueError('INCOMPLETE_CITATION_SCOPE')
        rendered='\n'.join(c['text']+' '+''.join('['+k+']' for k in c['citations']) for c in claims)
        if response.get('safe_output')!=rendered:raise ValueError('PUBLISHED_OUTPUT_MISMATCH')
        if response.get('model_support_review_passed') is not True:raise ValueError('SUPPORT_REVIEW_FLAG_MISSING')
        if response.get('generation_policy_identity',{}).get('name') in ('anchored_v3','anchored_v4','anchored_v5_exp'):
            groups=response.get('claim_supports',[])
            if len(groups)!=len(claims):raise ValueError('SUPPORT_COVERAGE')
            support_chars=0
            for i,(claim,group) in enumerate(zip(claims,groups)):
                spans=group.get('supports',[])
                if group.get('claim_index')!=i+1 or not 1<=len(spans)<=16:raise ValueError('SUPPORT_COVERAGE')
                if {s['evidence_key'] for s in spans}!=set(claim['citations']):raise ValueError('SUPPORT_COVERAGE')
                if len({(s['evidence_key'],s['quote']) for s in spans})!=len(spans):raise ValueError('DUPLICATE_SUPPORT')
                for s in spans:
                    q=s['quote'];key=s['evidence_key']
                    if not q.strip() or len(q)>400 or not any(q in original for original in lookup[key]['quotes']):raise ValueError('NONEXACT_SUPPORT')
                    if hashlib.sha256(q.encode()).hexdigest()!=s['quote_sha256']:raise ValueError('SUPPORT_HASH_MISMATCH')
                    if response['generation_policy_identity']['name'] in ('anchored_v4','anchored_v5_exp'):
                        idx,start,end=s.get('quote_index'),s.get('quote_start'),s.get('quote_end')
                        if (type(idx) is not int or not 0<=idx<len(lookup[key]['quotes']) or type(start) is not int or type(end) is not int
                            or not 0<=start<end<=len(lookup[key]['quotes'][idx]) or lookup[key]['quotes'][idx][start:end]!=q
                            or s.get('alignment') not in ('exact','source_whitespace')):raise ValueError('SUPPORT_SPAN_MISMATCH')
                    support_chars+=len(q)
                if not set(re.findall(r'\d+(?:\.\d+)?',claim['text']))<=set(re.findall(r'\d+(?:\.\d+)?','\n'.join(s['quote'] for s in spans))):raise ValueError('UNSUPPORTED_SUPPORT_NUMBER')
            if support_chars>2000:raise ValueError('SUPPORT_BUDGET')
    elif status!='EVIDENCE_ONLY' and (response.get('safe_output') or claims):raise ValueError('UNSAFE_FAILURE_OUTPUT')
    if blocks is None:return {'citation_contract':'passed','source_audit':'unavailable_without_snapshot_dir'}
    catalog={};details=response.get('excerpt_details',[])
    if (status=='GENERATED_ANSWER' or fallback) and not details:raise ValueError('SOURCE_DETAILS_MISSING')
    if len(details)>8 or sum(d['excerpt_chars'] for d in details)>response.get('excerpt_budget_chars_total',3500):raise ValueError('CONTEXT_BUDGET')
    for d in details:
        parts=[];snapshots=set()
        for sp in d['source_spans']:
            b=blocks[sp['block_id']];a,z=sp['start'],sp['end'];snapshots.add(b['snapshot_id'])
            if (b['snapshot_id'] not in scope or not b['indexable'] or b.get('kind') in ('table_unverified','unparsed','figure_unverified')
                or type(a) is not int or type(z) is not int or not 0<=a<z<=len(b['text']) or b.get('locator',{}).get('page')!=d['page']):raise ValueError('INVALID_SOURCE_SPAN')
            parts.append(b['text'][a:z])
        if len(snapshots)!=1:raise ValueError('MIXED_WINDOW_SNAPSHOTS')
        if d.get('structure_version'):
            refs=d.get('heading_spans',[]);path=d.get('heading_path',[])
            if len(refs)!=len(path):raise ValueError('HEADING_PROVENANCE_MISMATCH')
            for ref,title in zip(refs,path):
                original=blocks[ref['block_id']];start,end=ref['start'],ref['end']
                if (original['snapshot_id']!=next(iter(snapshots)) or not original['indexable']
                    or type(start) is not int or type(end) is not int or not 0<=start<end<=len(original['text'])
                    or original['text'][start:end].strip().strip('\u200b').strip()!=title):
                    raise ValueError('HEADING_PROVENANCE_MISMATCH')
        text='\n'.join(parts);a,z=d['source_window_start'],d['source_window_end']
        if type(a) is not int or type(z) is not int or not 0<=a<z<=len(text):raise ValueError('INVALID_WINDOW')
        q=text[a:z]
        if len(q)!=d['excerpt_chars'] or len(q)>response.get('excerpt_limit_chars',600) or hashlib.sha256(q.encode()).hexdigest()!=d['quote_sha256']:raise ValueError('QUOTE_HASH_MISMATCH')
        if d['block_id'] in catalog:raise ValueError('DUPLICATE_CONTEXT_ID')
        catalog[d['block_id']]={'quote':q,'page':d['page'],'snapshot_id':next(iter(snapshots)),
                               'structure':{k:d[k] for k in ('heading_path','content_kind','parent_id','structure_version') if k in d}}
    for e in extracts:
        original=catalog.get(e['block_id'])
        if not original or any(q not in original['quote'] for q in e['quotes']):raise ValueError('NONEXACT_CITATION')
        if (e['snapshot_id']!=original['snapshot_id'] or e.get('page')!=original['page']
            or e.get('locator',{}).get('page',original['page'])!=original['page']):raise ValueError('CITATION_PROVENANCE_MISMATCH')
        if any(e.get('locator',{}).get(k)!=v for k,v in original['structure'].items()):raise ValueError('CITATION_STRUCTURE_MISMATCH')
    return {'citation_contract':'passed','source_audit':'passed'}


def run(args):
    if args.context_policy and args.endpoint!='/diagnostics/answer':raise ValueError('CONTEXT_OVERRIDE_REQUIRES_EVALUATION_ENDPOINT')
    generation_policy=getattr(args,'generation_policy',None)
    if generation_policy and args.endpoint!='/diagnostics/answer':raise ValueError('GENERATION_OVERRIDE_REQUIRES_EVALUATION_ENDPOINT')
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    cases=json.loads(Path(args.cases).read_text(encoding='utf-8-sig'))
    output=Path(args.output);output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ)
    if args.env_file:
        for line in Path(args.env_file).read_text().splitlines():
            if '=' in line:
                k,v=line.split('=',1)
                if k in ('TECHDOC_BROWSER_USER','TECHDOC_BROWSER_PASSWORD'):env[k]=v
    user=env.get('TECHDOC_BROWSER_USER','');password=env.get('TECHDOC_BROWSER_PASSWORD','')
    headers={'Content-Type':'application/json'}
    if user or password:headers['Authorization']='Basic '+base64.b64encode((user+':'+password).encode()).decode()
    context=ssl.create_default_context(cafile=args.ca_file)
    def request(path,body=None):
        req=Request(config['base_url'].rstrip('/')+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),headers=headers)
        with urlopen(req,context=context,timeout=args.timeout) as r:return json.loads(r.read())
    docs=resolve_documents(config['documents'],request('/documents')['documents'])
    blocks=None
    if args.snapshot_dir:
        blocks={}
        for doc in docs.values():
            data=json.loads((Path(args.snapshot_dir)/(doc['snapshot_id']+'.json')).read_text())
            if data['metadata']['source_sha256']!=doc['source_sha256']:raise ValueError('SNAPSHOT_SOURCE_MISMATCH')
            blocks.update({b['block_id']:b for b in data['blocks']})
    report={'kind':'AUTOMATED_DEVELOPMENT_CHECKS_NOT_HUMAN_GOLD','independent_gold':False,
            'semantic_review_pending':True,'started_at_unix':time.time(),'cases_sha256':hashlib.sha256(Path(args.cases).read_bytes()).hexdigest(),
            'config_sha256':hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),'documents':docs,'health_before':request('/health'),
            'endpoint':args.endpoint,'context_policy_requested':args.context_policy,
            'generation_policy_requested':generation_policy,'cases':[]}
    if args.code_root:report['code_hashes']=build_manifest(args.code_root)
    def save():
        temp=output/'results.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(output/'results.json')
    save()
    for case in cases:
        scope=[docs[k]['snapshot_id'] for k in case['scope_documents']]
        row={'id':case['id'],'question':case['question'],'scope':scope,'expected_checklist':case.get('checklist',[]),'reference_answer':case.get('answer_draft'),
             'kind':case.get('kind'),'measurement_origin':'fresh_http_run','semantic_verdict':'PENDING_AI_REVIEW'}
        started=time.perf_counter()
        try:
            # Reference answers/checklists are deliberately never sent to /query.
            if args.code_root:
                if build_manifest(args.code_root)!=report['code_hashes']:raise ValueError('BUILD_CHANGED_DURING_RUN')
            body={'question':case['question'],'snapshot_ids':scope,'mode':args.mode}
            if args.context_policy:body['context_policy']=args.context_policy
            if generation_policy:body['generation_policy']=generation_policy
            response=request(args.endpoint,body)
            if args.endpoint=='/diagnostics/answer' and (response.get('evaluation_only') is not True or response.get('context_policy')!=args.context_policy):raise ValueError('EVALUATION_CONTEXT_MISMATCH')
            if generation_policy and response.get('generation_policy')!=generation_policy:raise ValueError('EVALUATION_GENERATION_MISMATCH')
            row['response']=response;row['audit']=audit_response(response,scope,blocks)
            is_refusal=response.get('status') in ('MODEL_ABSTAIN','ABSTAIN_SUPPORT_REVIEW') and not response.get('safe_output')
            row['response_contract_passed']=is_refusal if case.get('kind')=='unanswerable' else response.get('status')=='GENERATED_ANSWER'
        except HTTPError as exc:row['error_code']='HTTP_'+str(exc.code)
        except Exception as exc:row['error_code']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        row['elapsed_ms']=round((time.perf_counter()-started)*1000,3);report['cases'].append(row);save()
        print(row['id'],row.get('response',{}).get('status'),row['elapsed_ms'],row.get('error_code',''),flush=True)
    report['build_unchanged']=not args.code_root or build_manifest(args.code_root)==report['code_hashes']
    report['health_after']=request('/health');report['finished_at_unix']=time.time();save()
    lines=['# 自动规则评测结果','', '此报告只包含响应与来源合同检查；语义质量仍需AI审查，不能当作人工Gold。','',
           '| 题号 | 响应状态 | 耗时 | 来源核验 |','|---|---|---:|---|']
    for r in report['cases']:lines.append(f"| {r['id']} | {r.get('response',{}).get('status',r.get('error_code'))} | {r['elapsed_ms']/1000:.1f}秒 | {r.get('audit',{}).get('source_audit','未完成')} |")
    (output/'summary.md').write_text('\n'.join(lines),encoding='utf-8')
    return 0 if report['build_unchanged'] and all(r.get('response_contract_passed') and not r.get('error_code') for r in report['cases']) else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ['config','cases','output']:parser.add_argument('--'+field,required=True)
    parser.add_argument('--env-file');parser.add_argument('--ca-file');parser.add_argument('--snapshot-dir');parser.add_argument('--code-root')
    parser.add_argument('--timeout',type=float,default=240);parser.add_argument('--mode',choices=['answer'],default='answer')
    parser.add_argument('--endpoint',choices=['/query','/diagnostics/answer'],default='/query')
    parser.add_argument('--context-policy',choices=['legacy','structured'])
    parser.add_argument('--generation-policy',choices=['legacy','citation_v2','anchored_v3','anchored_v4'])
    return run(parser.parse_args())

if __name__=='__main__':raise SystemExit(main())
