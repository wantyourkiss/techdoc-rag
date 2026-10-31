"""Bounded, quote-only evidence display from retrieved source blocks.

Never generates facts: windows are verbatim from the validated original block.
Selection uses query words and retrieved rank; no gold labels or target quote.
This is *source-block contextual expansion*, not additional retrieval recall.
"""
from __future__ import annotations
from collections import defaultdict
import re

from .evidence_gate import Candidate

WORD_POS = re.compile(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', re.UNICODE)


def query_terms_with_positions(text: str):
    """Source-offset-preserving English/CJK terms with CamelCase decomposition."""
    for m in WORD_POS.finditer(text):
        raw=m.group()
        t=raw.lower()
        if '\u4e00' <= t[0] <= '\u9fff' and len(t)>1:
            for i in range(len(t)-1):
                yield t[i:i+2],m.start()+i,m.start()+i+2
        else:
            yield t,m.start(),m.end()
            # Preserve full identifier AND subwords so, e.g., query
            # FramebufferUpdate can match source 'framebuffer update'.
            off=0
            parts=re.split(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])',raw)
            if len(parts)>1:
                for part in parts:
                    if len(part)>1:
                        yield part.lower(),m.start()+off,m.start()+off+len(part)
                    off+=len(part)


STOP = frozenset('what which who when where why how is are was were do does did may can could should would have has had the a an in of to by and or for with from on at this that those these it its be as about still use used using type message rfb protocol'.split())
SENTENCE_BREAK = re.compile(r'[.!?。！？](?:\s+|$)')
CHINESE_QUESTION_WORDS=re.compile(r'为什么|哪些|什么|怎么|如何|为何')


def window_terms(question,policy='legacy'):
    if policy not in ('legacy','zh_questions_v1'):raise ValueError('BAD_WINDOW_POLICY')
    terms=list(query_terms_with_positions(question));q={t for t,_,_ in terms}
    low=set(STOP)
    if policy=='zh_questions_v1':
        quoted=[m.span() for m in re.finditer(r'"[^"]*"|“[^”]*”|‘[^’]*’|`[^`]*`',question)]
        spans=[m.span() for m in CHINESE_QUESTION_WORDS.finditer(question)
               if not any(a<=m.start() and m.end()<=z for a,z in quoted)]
        # Only occurrences inside a question phrase lose weight. Preserve a token
        # that also occurs as meaningful content elsewhere, especially negation.
        contextual={t for t,a,z in terms if not any(l<=a and z<=r for l,r in spans)}
        low|={t for t,a,z in terms if t not in contextual and not any(c in t for c in '不未无')
              and any(l<=a and z<=r for l,r in spans)}
    return q,q-low,q&low


def choose_window(text: str, question: str, cap: int = 260, *, policy='legacy') -> tuple[int, int]:
    """Bounded whole-sentence-biased window from a retrieved original block.

    Search starts at sentence boundaries, preferentially exposing a complete
    statement rather than a prefix or a mid-sentence clipped substring.
    Only query text and source text guide selection; no annotated quote input.
    """
    if not text: return 0, 0
    if len(text)<=cap: return 0,len(text)
    q,content,low=window_terms(question,policy)
    if not content or not any(t in content for t,_,_ in query_terms_with_positions(text)):
        return 0, cap
    boundaries=[0]
    boundaries.extend(m.end() for m in SENTENCE_BREAK.finditer(text) if m.end()<len(text))
    boundaries=sorted(set(min(len(text)-1, max(0,x)) for x in boundaries))
    starts=set(boundaries)
    # Very long sentences: additional windows anchored to meaningful query terms.
    for t,l,r in query_terms_with_positions(text):
        if t in content:
            starts.add(max(0,min(len(text)-cap,l-40)))
    # Always retain start/end as fallbacks for pages without sentence punctuation.
    starts.add(len(text)-cap)
    best=None
    for start in starts:
        end=min(start+cap,len(text))
        possible=[x for x in boundaries if start<x<=end]
        # A paragraph may end after a sentence; avoid clipping in the middle of
        # the next sentence when the previous is already useful.
        if possible and end<len(text):
            end=max(possible)
        if end<=start:continue
        window=text[start:end]
        present={t for t,_,_ in query_terms_with_positions(window)}
        informative=len(present & content)
        generic=len(present & low)
        complete=int(end==len(text) or end in boundaries)
        score=(informative,generic,complete,-start)
        if best is None or score>best[0]:best=(score,start,end)
    if best is None:return 0,cap
    start,end=best[1],best[2]
    # Avoid slicing inside words, which turns a real source quote into an
    # incomplete fragment. Bound movement and retain the fixed character cap.
    adjusted=start
    for _ in range(min(12,start)):
        if adjusted<=0 or text[adjusted-1].isspace(): break
        adjusted-=1
    slack=cap-(end-start)
    if adjusted<start and start-adjusted<=slack:
        start=adjusted
    elif adjusted<start:
        # At full budget, move forward to the next word boundary rather than
        # clipping a sentence at the far end to gain a few prefix characters.
        for k in range(start,min(end,start+12)):
            if k>0 and text[k-1].isspace():
                start=k
                break
    return start,end


def select_excerpts(hits: list[dict], by_block: dict, question: str, *, limit: int = 4,
                    chars_each: int = 260, chars_total: int = 1040,
                    required_snapshots: frozenset[str] | None = None,
                    prefer_rank: bool = False,window_policy='legacy') -> tuple[list[Candidate], list[dict]]:
    """Rank-preserving, block-deduplicated excerpt selection.

    Input hits are from the configured retriever. A chunk's source span must
    point to a real indexable original block with matching snapshot identity.
    The complete source block is read only for bounded contextual expansion.
    No separate retrieval or unlabeled external data is consulted.
    """
    ordered=[]
    seen=set()
    for rank, hit in enumerate(hits, 1):
        ch=hit['chunk']
        for sp in ch['source_spans']:
            bid=sp['block_id']
            b=by_block.get(bid)
            if not b or bid in seen or not b.get('indexable') or b['snapshot_id']!=ch['snapshot_id']:
                continue
            txt=b.get('text','')
            a,z=sp.get('start'),sp.get('end')
            if not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(txt):
                continue
            seen.add(bid)
            ordered.append((rank,b,sp))
    qset={t for t,_,_ in query_terms_with_positions(question)}
    scored=[]
    for rank,b,sp in ordered:
        aa,zz=choose_window(b['text'],question,chars_each,policy=window_policy)
        w=b['text'][aa:zz]
        overlap=len(qset & {t for t,_,_ in query_terms_with_positions(w)})
        scored.append((-overlap,rank,b['block_id'],b,sp,aa,zz))
    # A configured cross-encoder owns ranking; lexical overlap must not undo it.
    scored.sort(key=(lambda x: (x[3].get('kind') == 'heading', x[1], x[0], x[2])) if prefer_rank else None)
    # Reserve one source block for every explicitly selected document.
    # Keep the single-document rank/overlap behavior unchanged.
    if required_snapshots is not None:
        first=[];rest=[];covered=set()
        for item in scored:
            sid=item[3]['snapshot_id']
            if sid in required_snapshots and sid not in covered:
                first.append(item);covered.add(sid)
            else:
                rest.append(item)
        scored=first+rest
    out=[];detail=[];used=0
    for neg_overlap,rank,bid,b,sp,aa,zz in scored:
        if len(out)>=limit: break
        allowed=min(chars_each,chars_total-used)
        if allowed<=0: break
        a,z=(aa,zz) if allowed==chars_each else choose_window(b['text'],question,allowed,policy=window_policy)
        frag=b['text'][a:z]
        if not frag.strip():continue
        out.append(Candidate(b['block_id'],b['snapshot_id'],b.get('locator',{}).get('page'),(frag,)))
        used+=len(frag)
        detail.append({'block_id':b['block_id'],'rank_first_chunk':rank,
                       'source_window_start':a,'source_window_end':z,
                       'retrieved_span_start':sp['start'],'retrieved_span_end':sp['end'],
                       'expanded_from_source_block':a<sp['start'] or z>sp['end'],
                       'excerpt_chars':len(frag)})
    assert used<=chars_total and all(x['excerpt_chars']<=chars_each for x in detail)
    return out,detail
