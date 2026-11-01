"""Bounded contradiction checks, NOT a general semantic verifier.

Checks explicit quantities/property binding, ordered ranges, negation and guarded
actions against aligned cited sentences. Unknown paraphrases still need model review.
No external models, unit conversions, or assumptions about uncited context.
"""
import re
import unicodedata
from .threshold_guard import threshold_conflict,clean_markers

_UNITS={
    'millisecond':('milliseconds','millisecond','msec','ms','毫秒'),
    'second':('seconds','second','secs','sec','秒钟','秒'),
    'minute':('minutes','minute','mins','min','分钟'),
    'hour':('hours','hour','hrs','hr','小时'),
    'byte':('bytes','byte','字节'), 'kb':('kilobytes','kilobyte','kb'),
    'mb':('megabytes','megabyte','mb'), 'gb':('gigabytes','gigabyte','gb'),
    'count':('attempts','attempt','times','次'),
}
_UNIT_PATTERN='|'.join(re.escape(v) for values in _UNITS.values() for v in values)
_QUANTITY=re.compile(r'(?<![a-z0-9_.])(-?\d+(?:\.\d+)?)\s*('+_UNIT_PATTERN+r')(?![a-z])',re.I)
_UNIT_ALIAS={v:k for k,values in _UNITS.items() for v in values}
_STOP=set('the a an is are be been being must should may can will shall to of for in on at by as and or it this that only after before if when unless immediately alternatively otherwise not no never without permitted allowed required'.split())
_CONCEPTS={'服务器':'server','服务端':'server','备份':'backup','连接':'connection','超时时间':'timeout','超时':'timeout',
           '重试':'retry','访问':'access','远程':'remote','允许':'permit','许可':'permit','禁止':'permit','重启':'restart',
           '完成':'complete','之后':'after','以后':'after','之前':'before','以前':'before','仅':'only','只有':'only',
           '不':'not','未':'not','立即':'immediately','分钟':'minute','秒钟':'second','秒':'second',
           '本地':'local','本机':'local','或者':'or','否则':'otherwise','也可':'or'}
_NEG=re.compile(r"\b(?:not|no|never|cannot|without|forbidden|prohibited|denied)\b|n['’]t\b|禁止|不得|不能|不允许|不应|未获|不可")
_CONDITION=re.compile(r'\b(?:only\s+(?:after|before|if|when)|unless|provided\s+that)\b|仅当|只有|必须.{0,16}(?:后|前|时)|仅在')
_RANGE=re.compile(r'\b(?:between|from)\s*(-?\d+(?:\.\d+)?)\s*(?:and|to)\s*(-?\d+(?:\.\d+)?)\b|(-?\d+(?:\.\d+)?)\s*(?:至|到|～|~)\s*(-?\d+(?:\.\d+)?)')
_CONTRAST=re.compile(r'\s*,?\s*\b(?:but|whereas|however)\b\s*|[，,]?\s*(?:但是|然而|但)\s*')
_ALTERNATIVE=re.compile(r'\b(?:or|otherwise|alternatively)\b|或者|或是|否则|也可|亦可')

def _normalize(text):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',text).lower()).strip()

def _tokens(text):
    for word,value in sorted(_CONCEPTS.items(),key=lambda item:-len(item[0])):
        text=text.replace(word,' '+value+' ')
    words=re.findall(r'[a-z][a-z_-]*|[\u4e00-\u9fff]+',text)
    result=set()
    for word in words:
        if word in ('为','是','的','需','须','可以','应','必须'):continue
        if word in _STOP:continue
        word={'permitted':'permit','allows':'permit','allow':'permit','allowed':'permit',
              'retries':'retry','restarting':'restart','restarted':'restart','completes':'complete','completed':'complete'}.get(word,word)
        if re.fullmatch(r'[\u4e00-\u9fff]+',word):
            result.update(word[i:i+2] for i in range(max(1,len(word)-1)))
        else:result.add(word)
    return result

def _sentences(text):
    return [s.strip() for s in re.split(r'[。!?！？;；\n]|(?<!\d)\.(?!\d)',text) if s.strip()]

def _aligned(claim,sentence):
    a,b=_tokens(claim),_tokens(sentence)
    return bool(a and len(a&b)>=min(2,len(a)) and len(a&b)/len(a)>=.75)

def _contrast_units(text):
    # Only explicit contrast boundaries; do not split arbitrary commas, which
    # can detach a condition or a negation from the action it governs.
    return [part.strip(' ,，') for part in _CONTRAST.split(text) if part.strip(' ,，')]

def _branch_shape(text):
    # Keep logical qualifiers separately: bag-of-content-word inclusion alone
    # must not authorize a new unconditional alternative or changed comparator.
    condition=_CONDITION.search(text)
    return (bool(_NEG.search(text)),condition.group() if condition else None,
            tuple(re.findall(r'-?\d+(?:\.\d+)?',text)))

def _new_alternative(part,sources):
    branches=[b.strip(' ,，') for b in _ALTERNATIVE.split(part)]
    if len(branches)<2:return False
    supported=[b.strip(' ,，') for s in sources for b in _ALTERNATIVE.split(s)]
    for branch in branches:
        if not branch:return True
        words=_tokens(branch)
        if not any(branch==source or (words and words<=_tokens(source) and
                       _branch_shape(branch)==_branch_shape(source)) for source in supported):
            return True
    return False

def _quantities(sentence):
    for match in _QUANTITY.finditer(sentence):
        # Prefix stops at the previous clause/value, binding nearby property names.
        prefix=re.split(r'[,，:：;；]|\b(?:and|but)\b',sentence[:match.start()])[-1]
        yield match.group(1),_UNIT_ALIAS[match.group(2).lower()],_tokens(prefix)

def contradiction(claim,cited):
    """Return a stable reason for a recognizable contradiction, otherwise None."""
    claim=_normalize(claim);sentences=_sentences(_normalize(cited))
    if not claim or not sentences:return None
    threshold=threshold_conflict(claim,sentences,_tokens)
    if threshold:return threshold
    for part in _sentences(claim):
        # An exact supported sentence does not need inference by these heuristics.
        if any(part==s for s in sentences):
            continue
        def same_action(source):
            guard=_CONDITION.search(source);proposed=_CONDITION.search(part)
            return bool(guard and _aligned(part[:proposed.start()] if proposed else part,source[:guard.start()]))
        head=_ALTERNATIVE.split(part)[0]
        aligned=[s for s in sentences if _aligned(part,s) or same_action(s) or (head!=part and _aligned(head,s))]
        source_units=[unit for sentence in sentences for unit in _contrast_units(sentence)]
        for unit in _contrast_units(part):
            matches=[s for s in source_units if _aligned(unit,s)]
            if matches and all(bool(_NEG.search(clean_markers(s)))!=bool(_NEG.search(clean_markers(unit))) for s in matches):
                return 'NEGATION_CONTRADICTION'
        if aligned:
            guarded=[s for s in aligned if _CONDITION.search(s)]
            if guarded and len(guarded)==len(aligned):
                if _new_alternative(part,guarded):return 'CONDITION_UNSUPPORTED_BRANCH'
                # Require retained condition words and explicit condition content;
                # changing AFTER to BEFORE must not be treated as preservation.
                if not _CONDITION.search(part):return 'CONDITION_DROPPED'
                for source in guarded:
                    after=re.search(r'\b(only\s+(?:after|before|if|when)|unless|provided\s+that)\b',source)
                    proposed=re.search(r'\b(only\s+(?:after|before|if|when)|unless|provided\s+that)\b',part)
                    if after and proposed and after.group(1)!=proposed.group(1):return 'CONDITION_CHANGED'
                    if after and proposed:
                        options=[_tokens(branch) for branch in _ALTERNATIVE.split(source[after.end():])]
                        supplied=_tokens(part[proposed.end():])
                        if options and not any(required and len(required&supplied)/len(required)>=.75 for required in options):
                            return 'CONDITION_CHANGED'
        quantities=[q for s in sentences for q in _quantities(s)]
        for value,unit,anchors in _quantities(part):
            same_value=[q for q in quantities if q[0]==value]
            if same_value and not any(q[1]==unit for q in same_value):return 'UNIT_CONTRADICTION'
            if anchors:
                # Compare the most specifically matching property, not every number
                # in a passage; 15 for backup cannot justify 15 for server timeout.
                candidates=[q for q in quantities if q[2] and len(anchors&q[2])/len(anchors)>=.8]
                if candidates and not any(q[0]==value and q[1]==unit for q in candidates):
                    return 'ENTITY_VALUE_CONTRADICTION'
        ranges=[m.groups() for m in _RANGE.finditer(part)]
        source_ranges=[m.groups() for s in aligned for m in _RANGE.finditer(s)]
        for groups in ranges:
            pair=(groups[0],groups[1]) if groups[0] is not None else (groups[2],groups[3])
            actual=[(g[0],g[1]) if g[0] is not None else (g[2],g[3]) for g in source_ranges]
            if actual and pair not in actual:return 'RANGE_CONTRADICTION'
    return None
