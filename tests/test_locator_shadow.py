import copy,json
import pytest
from techdoc.locator_shadow import LocatorIntent,compare_context

DOCS={'a':{'filename':'Guide.pdf'},'b':{'filename':'Agent.pdf'},'outside':{'filename':'PRIVATE.pdf'}}
BLOCKS={'p1':{'snapshot_id':'a','locator':{'page':1}},'p8':{'snapshot_id':'a','locator':{'page':8}},'b3':{'snapshot_id':'b','locator':{'page':3}}}
def hits():return [
 {'score':9,'chunk':{'snapshot_id':'b','chunk_id':'b3','section':'第3章','source_spans':[{'block_id':'b3'}]}},
 {'score':8,'chunk':{'snapshot_id':'a','chunk_id':'p1','section':'第1章','source_spans':[{'block_id':'p1'}]}},
 {'score':7,'chunk':{'snapshot_id':'a','chunk_id':'p8','section':'第3章','source_spans':[{'block_id':'p8'}]}}]

@pytest.mark.parametrize('question,page,state',[
 ('《Guide》PDF第8页解释什么？',8,'physical_pdf'),('《Guide.pdf》PDF第八页',8,'physical_pdf'),
 ('PDF page 8 of "Guide.pdf"',8,'physical_pdf'),('《Guide》第8页',8,'unspecified_page'),
 ('《Guide.pdf》第8页',8,'unspecified_page'),('《PDF第8页.docx》',None,'none'),
 ('《Guide》PDF印刷第8页',8,'printed_unsupported'),('PDF第0页',None,'invalid'),
 ('PDF第十十页',None,'invalid'),('PDF第8页和第9页',None,'ambiguous')])
def test_page_origin_and_ordinal_are_conservative(question,page,state):
    plan=LocatorIntent.build(question,['a'],DOCS)
    assert plan.page==page and plan.page_state==state
    assert plan.public()['pdf_page']==(page if state=='physical_pdf' else None)
    assert not plan.public()['applied_to_answer']

@pytest.mark.parametrize('question,state',[('PDF page 8-10','range_unsupported'),('PDF page 8 to 10','range_unsupported'),('PDF page 8.5','none')])
def test_ranges_and_fractional_numbers_never_silently_pick_the_first_page(question,state):
    plan=LocatorIntent.build(question,['a'],DOCS)
    assert plan.page is None and plan.page_state==state

def test_exact_allowed_names_reorder_a_copy_without_changing_scores_or_membership():
    original=hits();before=copy.deepcopy(original)
    plan=LocatorIntent.build('《教程显示名》PDF第8页',['a','b'],DOCS,[('a','教程显示名','v1')])
    proposed,meta=plan.propose(original,BLOCKS)
    assert [h['chunk']['chunk_id'] for h in proposed]==['p8','p1','b3']
    assert original==before and sorted(h['score'] for h in proposed)==[7,8,9]
    assert meta['order_changed'] and meta['candidate_membership_preserved'] and meta['scope_preserved']
    assert '教程显示名' not in json.dumps(meta,ensure_ascii=False)

def test_no_lookup_or_matching_of_excluded_sources():
    class Scoped(dict):
        def __getitem__(self,key):assert key!='outside';return super().__getitem__(key)
    plan=LocatorIntent.build('《PRIVATE.pdf》PDF第8页',['a','b'],Scoped(DOCS))
    assert plan.file_state=='unresolved' and not plan.matched and plan.binding=='ambiguous_binding'
    _,public=plan.propose(hits(),BLOCKS)
    assert 'PRIVATE' not in json.dumps(public) and 'outside' not in json.dumps(public)
    with pytest.raises(ValueError,match='SCOPE'):
        plan.propose([{'chunk':{'snapshot_id':'outside'}}],BLOCKS)

def test_ambiguous_titles_bindings_and_non_pdf_do_not_guess():
    same={'a':{'filename':'Guide.pdf'},'b':{'filename':'Guide.docx'}}
    plan=LocatorIntent.build('《Guide》PDF第8页',['a','b'],same)
    assert plan.file_state=='ambiguous' and plan.target is None
    plan=LocatorIntent.build('比较《Guide.pdf》第3页和《Agent.pdf》第8页',['a','b'],DOCS)
    assert plan.binding=='ambiguous_binding' and plan.target is None and plan.page_state=='ambiguous'
    plan=LocatorIntent.build('PDF第8页',['b'],{'b':{'filename':'Guide.docx'}})
    assert plan.page_state=='non_pdf_source'

def test_chapter_marker_does_not_match_a_different_chapter():
    plan=LocatorIntent.build('《Guide》第三章',['a','b'],DOCS)
    ranked,meta=plan.propose(hits(),BLOCKS)
    assert ranked[0]['chunk']['chunk_id']=='p8' and not meta['structure_verified']
    assert plan.score({'chunk':{'snapshot_id':'a','section':'33. Other'}},BLOCKS)[2]==0

def test_observation_cap_does_not_truncate_formal_candidates():
    rows=[{'score':1,'chunk':{'snapshot_id':'a','chunk_id':str(i)}} for i in range(80)]
    proposed,meta=LocatorIntent.build('《Guide》',['a'],DOCS).propose(rows,BLOCKS)
    assert proposed==rows and len(proposed)==80 and meta['status']=='observation_budget_exceeded'

def test_context_summary_exposes_counts_not_proposed_quotes_or_ids():
    first=[{'chunk_id':'PRIVATE_CHUNK','source_spans':[{'block_id':'p1'}]}]
    second=[{'chunk_id':'OTHER_CHUNK','source_spans':[{'block_id':'b3'}]}]
    summary=compare_context({'applied_to_answer':False},first,second,BLOCKS,{'a'})
    assert summary['context_changed'] and not summary['required_sources_preserved']
    assert 'PRIVATE' not in json.dumps(summary) and 'OTHER' not in json.dumps(summary)
