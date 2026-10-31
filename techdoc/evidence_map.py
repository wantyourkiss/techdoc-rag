"""Question-to-citation lexical association, never semantic coverage certification."""
import re
import unicodedata

CONTRACT='question_evidence_v1'
_STOP=set('what why how is are the a an to of for and or does do can should please explain describe compare'.split())
_GENERIC={'为什','什么','如何','怎么','请问','说明','解释','介绍','需要','可以','是否','这个','哪些','以及'}

def parts(question):
    if not isinstance(question,str):return []
    values=[];current=[];closing=None
    pairs={'《':'》','“':'”','"':'"','`':'`'}
    for char in question:
        if closing:
            current.append(char)
            if char==closing:closing=None
        elif char in pairs:closing=pairs[char];current.append(char)
        elif char in '?？;；':
            if ''.join(current).strip():values.append(''.join(current).strip())
            current=[]
        else:current.append(char)
    if ''.join(current).strip():values.append(''.join(current).strip())
    if len(values)<2:return []
    # Preserve every explicit question even when the display has a bounded count.
    if len(values)>6:values=values[:5]+['；'.join(values[5:])]
    return values

def _terms(text):
    text=unicodedata.normalize('NFKC',text).casefold()
    result=set()
    for word in re.findall(r'[a-z][a-z0-9_-]*|[\u4e00-\u9fff]+',text):
        if re.fullmatch(r'[\u4e00-\u9fff]+',word):
            result.update(word[i:i+2] for i in range(len(word)-1) if word[i:i+2] not in _GENERIC)
        elif word not in _STOP:result.add(word)
    return result

def associate(question,sources):
    questions=parts(question)
    if not questions or not sources:return None
    # Sources are assembled from the run's already-authorized durable citations.
    texts=[(s['citation_id'],_terms('\n'.join(s['quotes']))) for s in sources]
    rows=[]
    for number,text in enumerate(questions,1):
        terms=_terms(text)
        ranked=sorted(enumerate(texts),key=lambda row:(-len(terms&row[1][1]),row[0]))
        linked=[record[0] for _,record in ranked if terms&record[1]][:4]
        rows.append({'number':number,'question':text[:350],'text_truncated':len(text)>350,
                     'state':'related_excerpt' if linked else 'unlinked','citation_ids':linked,
                     'semantic_support_verified':False})
    return {'contract':CONTRACT,'method':'shared_terms_v1','semantic_coverage_verified':False,'parts':rows}
