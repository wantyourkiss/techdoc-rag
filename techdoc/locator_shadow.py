"""Bounded, metadata-only locator observation. Never changes retrieval authority.

Only exact names inside the caller's selected snapshots are resolved. Page and
chapter hints are observations, not a verified document structure or hard filter.
The formal rank, model input and answer always use the original candidates.
"""
from dataclasses import dataclass
import re
import unicodedata

CONTRACT='locator_shadow_v1'
_TITLE=re.compile(r'《([^》\n]{1,200})》|“([^”\n]{1,200})”|"([^"\n]{1,200})"')
_ORD=r'[0-9零〇一二三四五六七八九十百]+'
_PAGE=re.compile(r'第\s*('+_ORD+r')\s*页|\b(?:pdf\s+)?page\s+(\d+)(?!\d)(?!\.\d)\b',re.I)
_CHAPTER=re.compile(r'第\s*('+_ORD+r')\s*章|\bchapter\s+(\d+)(?!\d)(?!\.\d)\b',re.I)

def _normal(text):return unicodedata.normalize('NFKC',str(text)).strip().casefold()

def _name(text):
    text=_normal(text)
    return re.sub(r'\.(?:pdf|docx?|md|txt|html?)$','',text)

def _number(text):
    if text.isdecimal():value=int(text)
    else:
        if not re.fullmatch(r'[零〇一二三四五六七八九]|[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九]百(?:零[一二三四五六七八九]|[一二三四五六七八九]?十[一二三四五六七八九]?)?',text):return None
        digits={c:i for i,c in enumerate('零一二三四五六七八九')};digits['〇']=0
        value=0;pending=0
        for c in text:
            if c in digits:pending=digits[c]
            elif c in ('十','百'):value+=(pending or 1)*(10 if c=='十' else 100);pending=0
            else:return None
        value+=pending
    return value if 1<=value<=10000 else None

def _hint(pattern,text):
    matches=list(pattern.finditer(text))
    if any(re.match(r'\s*(?:[-–—~～至到]|to\b)\s*\d',text[m.end():],re.I) for m in matches):return None,'range_unsupported'
    values=[_number(next(g for g in m.groups() if g is not None)) for m in matches]
    if not matches:return None,'none'
    if any(v is None for v in values):return None,'invalid'
    if len(set(values))!=1:return None,'ambiguous'
    return values[0],'single'

@dataclass(frozen=True)
class LocatorIntent:
    selected: tuple
    matched: frozenset
    binding: str
    file_state: str
    title_count: int
    unresolved_count: int
    ambiguous_count: int
    page: int | None
    page_state: str
    chapter: int | None
    chapter_state: str
    target: str | None

    @classmethod
    def build(cls,question,selected,documents,labels=()):
        selected=tuple(selected)
        if not selected or len(selected)!=len(set(selected)):raise ValueError('LOCATOR_BAD_SCOPE')
        text=_normal(question)
        titles=list(dict.fromkeys(_name(next(g for g in m.groups() if g is not None)) for m in _TITLE.finditer(text)))
        # Do not even enumerate document names outside the authorized scope.
        display={row[0]:row[1] for row in labels if row[0] in selected} if titles else {}
        names={sid:{_name(documents[sid].get('filename','')),_name(display.get(sid,''))}-{''} for sid in selected} if titles else {}
        matches=set();ambiguous=unresolved=0
        for title in titles:
            found=[sid for sid in selected if title in names[sid]]
            if not found:unresolved+=1
            elif len(found)>1:ambiguous+=1
            else:matches.add(found[0])
        file_state='none' if not titles else ('unresolved' if unresolved else ('ambiguous' if ambiguous else 'resolved'))
        locator_text=_TITLE.sub(' ',text)
        page,page_state=_hint(_PAGE,locator_text);chapter,chapter_state=_hint(_CHAPTER,locator_text)
        if page_state=='single':
            if re.search(r'印刷|书面页码|printed\s+page',locator_text):page_state='printed_unsupported'
            elif re.search(r'(?<![a-z0-9_])pdf(?![a-z0-9_])',locator_text,re.I):page_state='physical_pdf'
            else:page_state='unspecified_page'
        target=None;binding='none'
        if page_state!='none' or chapter_state!='none':
            if titles and file_state=='resolved' and len(matches)==1:target=next(iter(matches));binding='single_source'
            elif not titles and len(selected)==1:target=selected[0];binding='single_source'
            else:binding='ambiguous_binding'
        if target and page_state=='physical_pdf' and not _normal(documents[target].get('filename','')).endswith('.pdf'):
            page_state='non_pdf_source'
        return cls(selected,frozenset(matches),binding,file_state,len(titles),unresolved,ambiguous,
                   page,page_state,chapter,chapter_state,target)

    def public(self):
        # Counts/states only: no raw question, unknown name, file path, text or IDs.
        return {'contract':CONTRACT,'mode':'shadow','applied_to_answer':False,'extra_model_calls':0,
          'observation_scope':'existing_post_retrieval_candidates','file_hint_state':self.file_state,
          'file_hint_count':self.title_count,'matched_source_count':len(self.matched),
          'unresolved_hint_count':self.unresolved_count,'ambiguous_hint_count':self.ambiguous_count,
          'binding':self.binding,'pdf_page':self.page if self.page_state=='physical_pdf' else None,
          'page_hint_state':self.page_state,'chapter':self.chapter,'chapter_hint_state':self.chapter_state,
          'structure_verified':False}

    def score(self,hit,blocks):
        chunk=hit['chunk'];sid=chunk['snapshot_id']
        named=int(self.file_state=='resolved' and sid in self.matched)
        page=chapter=0
        if sid==self.target:
            pages={blocks[s['block_id']].get('locator',{}).get('page') for s in chunk.get('source_spans',[]) if s.get('block_id') in blocks}
            if self.page_state=='physical_pdf':page=int(pages=={self.page})
            if self.chapter_state=='single':
                section=_normal(chunk.get('section','')).lstrip('# ')
                if re.search(r'第\s*'+str(self.chapter)+r'\s*章|^'+str(self.chapter)+r'(?:[.、\s]|$)',section):chapter=1
                else:
                    match=_CHAPTER.search(section)
                    if match:chapter=int(_number(next(g for g in match.groups() if g is not None))==self.chapter)
        return named,page,chapter

    def propose(self,hits,blocks):
        if any(h['chunk']['snapshot_id'] not in self.selected for h in hits):raise ValueError('LOCATOR_CANDIDATE_SCOPE_MISMATCH')
        result=self.public();result['candidate_count']=len(hits)
        if len(hits)>64:
            result.update(status='observation_budget_exceeded',order_changed=False,scope_preserved=True)
            return list(hits),result
        active=self.file_state=='resolved' or (self.target and (self.page_state=='physical_pdf' or self.chapter_state=='single'))
        if not active:
            result.update(status='no_resolved_hint',order_changed=False,scope_preserved=True)
            return list(hits),result
        # Stable secondary order, no score changes and no in-place mutation.
        proposed=sorted(hits,key=lambda h:tuple(-value for value in self.score(h,blocks)))
        result.update(status='observed',order_changed=[h['chunk']['chunk_id'] for h in hits]!=[h['chunk']['chunk_id'] for h in proposed],
                      scope_preserved=True,candidate_membership_preserved=True,
                      candidate_location_matches=sum(any(self.score(h,blocks)[1:]) for h in hits))
        return proposed,result

def compare_context(summary,baseline,proposed,blocks,required=frozenset()):
    # Only aggregate diagnostics are persisted. Proposed quotes/spans are never
    # published as answer citations, and are never supplied to the model.
    def sources(details):
        identifiers={span['block_id'] for d in details for span in d.get('source_spans',[])}
        identifiers.update(d['block_id'] for d in details if d.get('block_id'))
        return {blocks[ident]['snapshot_id'] for ident in identifiers if ident in blocks}
    value=dict(summary)
    actual=sources(baseline);alternative=sources(proposed)
    value.update(context_changed=baseline!=proposed,
                 baseline_context_count=len(baseline),proposed_context_count=len(proposed),
                 baseline_source_count=len(actual),proposed_source_count=len(alternative),
                 required_sources_preserved=set(required)<=alternative)
    return value
