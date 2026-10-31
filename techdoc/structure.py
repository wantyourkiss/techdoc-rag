"""Derived parent/leaf context over immutable blocks; retrieval index stays intact.

PDF headings are conservative lexical hints, never verified visual structure.
Every character in a context quote is reconstructed from original block offsets.
"""
from collections import defaultdict
from hashlib import sha256
import json,re
from .evidence_gate import Candidate,EvidenceGate
from .excerpts import choose_window
from .context_windows import identifier_char

VERSION='source_structure_v1'
BAD_KINDS={'table_unverified','figure_unverified','unparsed'}


def identity(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]


def append_span(spans,bid,a,z):
    if spans and spans[-1]['block_id']==bid and spans[-1]['end']==a:spans[-1]['end']=z
    else:spans.append({'block_id':bid,'start':a,'end':z})


def reconstruct(spans,blocks):
    return '\n'.join(blocks[s['block_id']]['text'][s['start']:s['end']] for s in spans)


def heading_level(text,block):
    if len(text)>120:return None
    if block.get('kind')=='heading':return max(1,min(6,int(block.get('heading_level',1))))
    # Numbered procedure steps are not section titles. On PDFs only, accept
    # hierarchical numbering or a top-level technical title with a separator.
    if block.get('locator',{}).get('page') is not None:
        m=re.match(r'^(\d+(?:\.\d+)+)\s+\S',text)
        if m:return min(6,m[1].count('.')+1)
        if re.match(r'^\d+[.、]\s*(?:[A-Za-z][A-Za-z0-9 +_-]{1,45}\s*[-—（(]|(?:Agent|RAG|ReAct)(?=[\u4e00-\u9fff\s（(-]))',text):return 1
        if re.match(r'^[一二三四五六七八九十]+、\S',text):return 1
    return None


class StructureCatalog:
    def __init__(self,blocks,*,leaf_cap=550,parent_cap=1800):
        if not 100<=leaf_cap<=600 or not leaf_cap<=parent_cap<=4000:raise ValueError('BAD_STRUCTURE_BUDGET')
        self.blocks={b['block_id']:b for b in blocks}
        if len(self.blocks)!=len(blocks):raise ValueError('STRUCTURE_DUPLICATE_BLOCK')
        self.parents={};self.leaves={};self.intervals=defaultdict(list);self.children={}
        self.version=VERSION;self.leaf_cap=leaf_cap;self.parent_cap=parent_cap
        active=[];key=None;path=[];head_refs=[];roles=set();stack=[];snapshot=None;fence=None;group=0;last_page=None

        def flush():
            nonlocal active,roles
            if not active:return
            sid,page,section,_,code_group=key
            spans=[dict(s) for s in active];pid='parent_'+identity([VERSION,parent_cap,leaf_cap,key,spans])
            kinds=roles-{'heading'}
            kind=next(iter(kinds)) if len(kinds)==1 else ('mixed' if kinds else 'heading')
            parent={'parent_id':pid,'root_id':'root_'+identity([VERSION,sid]),'snapshot_id':sid,
                    'page':page,'section':section,'heading_path':list(path),'heading_spans':[dict(s) for s in head_refs],
                    'content_kind':kind,'source_spans':spans,'structure_version':VERSION,
                    'structure_verified':False,'code_group':code_group}
            self.parents[pid]=parent;self.children[pid]=[]
            current=[]
            def leaf():
                nonlocal current
                if not current:return
                text=reconstruct(current,self.blocks);lid='leaf_'+identity([pid,current,text])
                obj={**parent,'chunk_id':lid,'source_spans':[dict(s) for s in current],
                     'index_text':text,'ordinal':len(self.children[pid])}
                self.leaves[lid]=obj;self.children[pid].append(lid)
                for s in current:self.intervals[s['block_id']].append((s['start'],s['end'],lid))
                current=[]
            for sp in spans:
                pos=sp['start'];t=self.blocks[sp['block_id']]['text']
                while pos<sp['end']:
                    used=len(reconstruct(current,self.blocks)) if current else 0
                    sep=1 if current and current[-1]['block_id']!=sp['block_id'] else 0
                    room=leaf_cap-used-sep
                    if room<40 and current:leaf();continue
                    end=min(sp['end'],pos+room)
                    if end<sp['end']:
                        at=max(t.rfind('\n',pos+20,end),t.rfind(' ',pos+20,end))
                        if at>pos+20:end=at+1
                    append_span(current,sp['block_id'],pos,end);pos=end
                    if len(reconstruct(current,self.blocks))>=leaf_cap-20:leaf()
            leaf();active=[];roles=set()

        for b in blocks:
            sid=b['snapshot_id']
            if sid!=snapshot:
                flush();snapshot=sid;stack=[];fence=None;group=0;key=None;last_page=None
            page=b.get('locator',{}).get('page')
            if page is not None and page!=last_page:
                # Unverified PDF layout cannot prove that a title from a
                # previous page still governs text on this page.
                flush();stack=[];fence=None;key=None;group+=1
            last_page=page
            if not b.get('indexable') or not b.get('text') or b.get('kind') in BAD_KINDS:
                flush();key=None;group+=1;continue
            pos=0;block_list=bool(re.match(r'^[•◦*+-]\s*',b['text'])) or b.get('kind')=='list'
            for line in b['text'].splitlines(keepends=True):
                a,z=pos,pos+len(line);pos=z;text=line.strip().strip('\u200b').strip()
                if not text and fence is None and b.get('kind')!='code':continue
                mark=re.match(r'^(`{3,}|~{3,})(.*)$',text)
                was_code=fence is not None
                if fence is None and mark:
                    flush();group+=1;fence=(mark[1][0],len(mark[1]))
                closing=was_code and mark and mark[1][0]==fence[0] and len(mark[1])>=fence[1] and not mark[2].strip()
                is_code=was_code or fence is not None or b.get('kind')=='code'
                level=None if is_code else heading_level(text,b)
                if level is not None:
                    flush();stack=[h for h in stack if h[0]<level]
                    stack.append((level,text,{'block_id':b['block_id'],'start':a,'end':z}))
                next_path=[h[1] for h in stack];next_refs=[h[2] for h in stack]
                next_key=(sid,b.get('locator',{}).get('page'),b.get('section',''),tuple(next_path),group if is_code else None)
                if key!=next_key:flush();key=next_key
                path=next_path;head_refs=next_refs
                role='code' if is_code else ('heading' if level else ('list' if block_list else 'prose'))
                # Bound even a single very long original line without inventing text.
                for start in range(a,z,parent_cap):
                    end=min(z,start+parent_cap)
                    if active and len(reconstruct(active,self.blocks))+(end-start)+1>parent_cap:flush()
                    append_span(active,b['block_id'],start,end);roles.add(role)
                if closing:flush();fence=None;group+=1
        flush()
        self.fingerprint=identity([VERSION,leaf_cap,parent_cap,list(self.parents)])

    @classmethod
    def from_previous(cls,sources,previous,reusable):
        """Reuse complete unchanged snapshot trees; materialize changed trees only."""
        value=object.__new__(cls)
        value.version=VERSION;value.leaf_cap=previous.leaf_cap;value.parent_cap=previous.parent_cap
        fields=('blocks','parents','leaves','intervals','children');groups={name:defaultdict(dict) for name in fields}
        for name in fields:
            for key,item in getattr(previous,name).items():
                if name=='intervals':sid=previous.blocks[key]['snapshot_id']
                elif name=='children':sid=previous.parents[key]['snapshot_id']
                else:sid=item['snapshot_id']
                groups[name][sid][key]=item
            setattr(value,name,{})
        for sid in sorted(sources):
            changed=None if sid in reusable else cls(sources[sid],leaf_cap=value.leaf_cap,parent_cap=value.parent_cap)
            for name in fields:
                incoming=groups[name][sid] if changed is None else getattr(changed,name)
                target=getattr(value,name)
                if target.keys() & incoming.keys():raise ValueError('STRUCTURE_DUPLICATE_BLOCK')
                target.update(incoming)
        value.fingerprint=identity([VERSION,value.leaf_cap,value.parent_cap,list(value.parents)])
        return value

    def stats(self):
        return {'version':VERSION,'fingerprint':self.fingerprint,'parents':len(self.parents),'leaves':len(self.leaves),
                'roots':len({p['root_id'] for p in self.parents.values()}),
                'leaf_chars':self.leaf_cap,'parent_chars':self.parent_cap,'retrieval_index_changed':False}

    def context(self,hits,chunks,blocks,question,selected,*,budget=3500,limit=8,required_snapshots=None,window_policy='legacy'):
        primary=[];secondary=[];seen=set();retrieved={};legacy={c['chunk_id']:c for c in chunks}
        groups=[]
        for hit in hits:
            ch=hit['chunk']
            if ch['snapshot_id'] not in selected:raise ValueError('CONTEXT_SCOPE_MISMATCH')
            if legacy.get(ch['chunk_id'])!=ch:raise ValueError('STRUCTURE_UNKNOWN_RETRIEVAL_CHUNK')
            if reconstruct(ch['source_spans'],blocks)!=ch['index_text']:raise ValueError('CONTEXT_TEXT_MISMATCH')
            overlaps={}
            for sp in ch['source_spans']:
                for a,z,lid in self.intervals.get(sp['block_id'],[]):
                    if a<sp['end'] and sp['start']<z and self.leaves[lid]['content_kind']!='heading':
                        overlaps[lid]=overlaps.get(lid,0)+min(z,sp['end'])-max(a,sp['start'])
                        retrieved.setdefault(lid,ch['chunk_id'])
            groups.append(sorted(overlaps,key=lambda lid:-overlaps[lid]))
        # Preserve a body window for EACH ranked hit before spending slots on
        # split fragments or parent expansion. Heading-only leaves are metadata.
        for group in groups:
            if group and group[0] not in seen:seen.add(group[0]);primary.append(group[0])
        for group in groups:
            for lid in group:
                if lid not in seen:seen.add(lid);secondary.append(lid)
        primary+=secondary
        expanded=[]
        for lid in primary:
            ch=self.leaves[lid];siblings=self.children[ch['parent_id']];i=ch['ordinal']
            for j in (i-1,i+1):
                if 0<=j<len(siblings) and siblings[j] not in seen:
                    n=siblings[j];seen.add(n);expanded.append(n);retrieved[n]=retrieved[lid]
        ordered=primary+expanded;first=[];rest=[];covered=set()
        for lid in ordered:
            sid=self.leaves[lid]['snapshot_id']
            required=selected if required_snapshots is None else required_snapshots
            if sid in required and sid not in covered:covered.add(sid);first.append(lid)
            else:rest.append(lid)
        catalog=dict(blocks);candidates=[];details=[];used=0
        for lid in first+rest:
            ch=self.leaves[lid]
            if ch['snapshot_id'] not in selected:raise ValueError('CONTEXT_SCOPE_MISMATCH')
            text=reconstruct(ch['source_spans'],blocks)
            if text!=ch['index_text']:raise ValueError('CONTEXT_TEXT_MISMATCH')
            for s in ch['source_spans']:
                b=blocks[s['block_id']]
                if not b['indexable'] or b.get('kind') in BAD_KINDS or b['snapshot_id']!=ch['snapshot_id'] or b.get('locator',{}).get('page')!=ch['page']:
                    raise ValueError('CONTEXT_SOURCE_SPAN_INVALID')
            left=budget-used
            if left<80 or len(candidates)>=limit:break
            a,z=choose_window(text,question,min(600,left),policy=window_policy)
            # Leaf boundaries are character budgets, not identifier boundaries.
            # Trim partial ASCII identifiers against the ORIGINAL block too.
            first_span,last_span=ch['source_spans'][0],ch['source_spans'][-1]
            first_text=blocks[first_span['block_id']]['text'];start=first_span['start']
            last_text=blocks[last_span['block_id']]['text'];end=last_span['end']
            if a==0 and start>0 and identifier_char(first_text[start-1]) and identifier_char(first_text[start]):
                while a<z and identifier_char(text[a]):a+=1
            if z==len(text) and end<len(last_text) and identifier_char(last_text[end-1]) and identifier_char(last_text[end]):
                while a<z and identifier_char(text[z-1]):z-=1
            while 0<a<z and identifier_char(text[a-1]) and identifier_char(text[a]):a+=1
            while a<z<len(text) and identifier_char(text[z-1]) and identifier_char(text[z]):z-=1
            quote=text[a:z]
            if not quote.strip():continue
            bid='context_'+identity([VERSION,lid])
            provenance={k:ch[k] for k in ('root_id','parent_id','heading_path','heading_spans','content_kind','structure_version','structure_verified')}
            locator={'page':ch['page'],'chunk_id':lid,'source_spans':ch['source_spans'],
                     'derived_from_original_spans':True,**provenance}
            catalog[bid]={'block_id':bid,'snapshot_id':ch['snapshot_id'],'text':text,'indexable':True,
                          'kind':'derived_source_window','section':ch['section'],'locator':locator}
            candidates.append(Candidate(bid,ch['snapshot_id'],ch['page'],(quote,)))
            details.append({'block_id':bid,'chunk_id':lid,'retrieval_chunk_id':retrieved[lid],'page':ch['page'],
                            'source_spans':ch['source_spans'],'excerpt_chars':len(quote),'source_window_start':a,
                            'source_window_end':z,'quote_sha256':sha256(quote.encode()).hexdigest(),**provenance})
            used+=len(quote)
        return EvidenceGate(list(catalog.values())),candidates,details
