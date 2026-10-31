"""Deterministic, scoped retrieval plans. Compact candidates remain shadow-only."""
from dataclasses import dataclass, field
import re
from .retrieval import query_facets

PLAN_VERSION = 'scoped_query_plan_v2'


@dataclass(frozen=True)
class QueryPlan:
    profile: str
    facets: tuple[str, ...] = field(repr=False)
    snapshots: tuple[str, ...]
    policy: str
    compact_eligible: bool
    answer_pipeline: bool
    reranker_enabled: bool
    knowledge_scope: object | None = field(default=None,repr=False)

    @classmethod
    def build(cls, question, snapshots, *, policy='shadow', answer=True, hybrid=True, reranker=True,knowledge_scope=None,retrieval_topic=''):
        if policy not in ('off', 'shadow'):
            raise ValueError('BAD_QUERY_PLAN_POLICY')
        selected=tuple(snapshots)
        if knowledge_scope is not None:
            from .knowledge_scope import KnowledgeQueryScope
            if not isinstance(knowledge_scope,KnowledgeQueryScope) or selected!=knowledge_scope.snapshots:
                raise ValueError('BAD_KNOWLEDGE_PLAN_SCOPE')
        elif not 1<=len(selected)<=4 or len(selected)!=len(set(selected)):
            raise ValueError('BAD_PLAN_SCOPE')
        facets=tuple(query_facets(question)) if answer else (question,)
        if retrieval_topic:
            if not isinstance(retrieval_topic,str) or len(retrieval_topic)>96:raise ValueError('BAD_RETRIEVAL_TOPIC')
            facets=tuple(part+' '+retrieval_topic for part in facets)
        comparison=bool(re.search(r'比较|区别|共同|不同|异同|对比|\b(?:compare|difference|common|versus|vs)\b',question,re.I))
        definition=bool(re.search(r'什么是|是什么|指什么|含义|的定义|\bwhat (?:is|are)\b|^\s*define\b',question,re.I))
        # Mentioning "define tools" is not a definition question. Causal and
        # procedural requests retain the full general/complex policy.
        if re.search(r'为什么|为何|如何|怎么|\b(?:why|how)\b',question,re.I):definition=False
        if len(selected)>1:profile='multi_source_comparison' if comparison else 'multi_source'
        elif len(facets)>1:profile='multipart'
        elif comparison:profile='comparison'
        elif definition and len(question)<=160:profile='definition'
        else:profile='general'
        if knowledge_scope is not None:
            profile='library_comparison' if knowledge_scope.required_documents else 'library_search'
        eligible=answer and hybrid and reranker and profile=='definition'
        return cls(profile,facets,selected,policy,eligible,answer,reranker,knowledge_scope)

    def public(self):
        return {'version':'knowledge_query_plan_v1' if self.knowledge_scope else PLAN_VERSION,'policy':self.policy,'profile':self.profile,
                'snapshot_ids':list(self.snapshots),'facet_count':len(self.facets),
                'candidate_pruning_active':False,'compact_shadow_eligible':self.compact_eligible,
                'proposed_candidates_per_source':12 if self.compact_eligible else None,
                'rerank_per_source':5 if self.reranker_enabled else None,
                'context_windows':8 if self.answer_pipeline else 4,
                'context_chars':3500 if self.answer_pipeline else 1040,
                **(self.knowledge_scope.public() if self.knowledge_scope else {})}

    def proposed_candidates(self, hits):
        # Plans never infer filenames or widen the caller's validated scope.
        if any(h['chunk']['snapshot_id'] not in self.snapshots for h in hits):
            raise ValueError('PLAN_CANDIDATE_SCOPE_MISMATCH')
        if not self.compact_eligible:return list(hits)
        counts={};out=[]
        for h in hits:
            sid=h['chunk']['snapshot_id'];counts[sid]=counts.get(sid,0)+1
            if counts[sid]<=12:out.append(h)
        return out


def shadow_ranking(proposed, scored, *, per_snapshot=5):
    """Reuse actual baseline scores; no second inference and no cache dependency."""
    lookup={h['chunk_id']:h for h in scored}
    if len(lookup)!=len(scored):raise ValueError('SHADOW_DUPLICATE_SCORE')
    ranked=[]
    for hit in proposed:
        ch=hit['chunk'];score=lookup.get(ch['chunk_id'])
        if not score or score['snapshot_id']!=ch['snapshot_id']:
            raise ValueError('SHADOW_SCORE_SCOPE_MISMATCH')
        ranked.append({**hit,'retrieval_score':hit['score'],'score':score['score']})
    ranked.sort(key=lambda h:(-h['score'],h['chunk']['chunk_id']))
    counts={};out=[]
    for h in ranked:
        sid=h['chunk']['snapshot_id'];counts[sid]=counts.get(sid,0)+1
        if counts[sid]<=per_snapshot:out.append(h)
    return out
