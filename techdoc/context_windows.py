"""Reconstruct cited chunk windows from validated original spans, never model text."""
from hashlib import sha256
from .excerpts import choose_window
from .evidence_gate import Candidate, EvidenceGate


def identifier_char(c):
    return c.isascii() and (c.isalnum() or c=='_')


def complete_identifier_edges(spans, blocks, max_extra=64):
    """Extend source offsets only within the same validated original blocks."""
    spans=[dict(s) for s in spans]
    first,last=spans[0],spans[-1]
    t=blocks[first['block_id']]['text'];a=first['start'];original=a
    while a>0 and a<len(t) and identifier_char(t[a-1]) and identifier_char(t[a]) and original-a<max_extra:a-=1
    if a==0 or not (identifier_char(t[a-1]) and identifier_char(t[a])):first['start']=a
    t=blocks[last['block_id']]['text'];z=last['end'];original=z
    while 0<z<len(t) and identifier_char(t[z-1]) and identifier_char(t[z]) and z-original<max_extra:z+=1
    if z==len(t) or not (identifier_char(t[z-1]) and identifier_char(t[z])):last['end']=z
    return spans


def chunk_context(hits, chunks, blocks, question, selected, *, budget=3500, limit=8,required_snapshots=None,window_policy='legacy'):
    catalog=dict(blocks)
    ordered=[]
    lookup={c['chunk_id']:i for i,c in enumerate(chunks)}
    seen=set()
    for hit in hits:
        ch=hit['chunk'];index=lookup[ch['chunk_id']]
        neighbors=[ch]
        # Parent context remains on the same source page/section and snapshot.
        for pos in (index-1,index+1):
            if 0<=pos<len(chunks):
                n=chunks[pos]
                if n['snapshot_id']==ch['snapshot_id'] and n['section']==ch['section']:
                    neighbors.append(n)
        for n in neighbors:
            if n['chunk_id'] not in seen:
                seen.add(n['chunk_id']);ordered.append(n)
    # Keep at least one window from each explicitly selected document.
    first=[];rest=[];covered=set()
    for ch in ordered:
        required=selected if required_snapshots is None else required_snapshots
        if ch['snapshot_id'] in required and ch['snapshot_id'] not in covered:
            covered.add(ch['snapshot_id']);first.append(ch)
        else:rest.append(ch)
    candidates=[];details=[];used=0
    for ch in first+rest:
        if ch['snapshot_id'] not in selected:
            raise ValueError('CONTEXT_SCOPE_MISMATCH')
        spans=ch['source_spans'];parts=[];pages=set()
        for span in spans:
            b=blocks.get(span['block_id']);a,z=span['start'],span['end']
            if (not b or not b['indexable'] or b.get('kind') in ('table_unverified','unparsed','figure_unverified') or b['snapshot_id']!=ch['snapshot_id']
                or not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(b['text'])):
                raise ValueError('CONTEXT_SOURCE_SPAN_INVALID')
            parts.append(b['text'][a:z]);pages.add(b.get('locator',{}).get('page'))
        text='\n'.join(parts)
        if text!=ch['index_text']:
            raise ValueError('CONTEXT_TEXT_MISMATCH')
        if len(pages)!=1:raise ValueError('CONTEXT_PAGE_BOUNDARY')
        spans=complete_identifier_edges(spans,blocks)
        text='\n'.join(blocks[s['block_id']]['text'][s['start']:s['end']] for s in spans)
        remaining=budget-used
        if remaining<80 or len(candidates)>=limit:break
        a,z=choose_window(text,question,min(600,remaining),policy=window_policy)
        # A remaining character budget may still cut a word: trim, never invent.
        while 0<a<z and identifier_char(text[a-1]) and identifier_char(text[a]):a+=1
        while a<z<len(text) and identifier_char(text[z-1]) and identifier_char(text[z]):z-=1
        quote=text[a:z]
        if not quote.strip():continue
        bid='context_'+sha256((ch['snapshot_id']+ch['chunk_id']).encode()).hexdigest()[:24]
        page=next(iter(pages))
        catalog[bid]={'block_id':bid,'snapshot_id':ch['snapshot_id'],'text':text,
            'indexable':True,'kind':'derived_source_window','section':ch['section'],
            'locator':{'page':page,'chunk_id':ch['chunk_id'],'source_spans':spans,
                       'derived_from_original_spans':True}}
        candidates.append(Candidate(bid,ch['snapshot_id'],page,(quote,)))
        details.append({'block_id':bid,'chunk_id':ch['chunk_id'],'page':page,
                        'source_spans':spans,'excerpt_chars':len(quote),
                        'source_window_start':a,'source_window_end':z,
                        'quote_sha256':sha256(quote.encode()).hexdigest()})
        used+=len(quote)
    return EvidenceGate(list(catalog.values())),candidates,details
