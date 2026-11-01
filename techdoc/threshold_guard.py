"""Explicit, same-property threshold entailment checks; no unit conversion."""
import re
from decimal import Decimal

_FORMS={
 'le':r'no\s+more\s+than|not\s+(?:more|greater)\s+than|at\s+most|up\s+to|不(?:得)?(?:超过|高于|大于)|不多于|至多|最多|小于等于|≤|<=',
 'ge':r'no\s+less\s+than|not\s+(?:less|fewer)\s+than|at\s+least|不(?:得)?(?:低于|少于|小于)|不少于|至少|最少|大于等于|≥|>=',
 'lt':r'less\s+than|fewer\s+than|lower\s+than|below|under|小于|低于|少于|<',
 'gt':r'greater\s+than|more\s+than|higher\s+than|above|over|大于|高于|超过|>',
}
_MARK=re.compile('|'.join('(?P<'+kind+'>'+pattern+')' for kind,pattern in _FORMS.items()),re.I)
_VALUE=re.compile(r'\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*([a-z%]+|毫秒|秒钟|分钟|小时|秒|次)?',re.I)
_EXACT=re.compile(r'\b(?:is|are|equals?|equal\s+to|exactly|precisely|set\s+to)\s+(?:(?:exactly|precisely)\s+)?|(?:恰好|正好|固定|确切|等于|为)(?:是|为)?\s*|(?<![<>!=])=(?!=)|[:：]',re.I)
_LIMIT=re.compile(r'\b(?:limit|bound|maximum|minimum|ceiling|floor|cap)\b|上限|下限|最大|最小|最多|至少|至多|最少',re.I)
_NEGATIVE_PREFIX=re.compile(r'\b(?:not|no|never)\b|不是|不为|并非|不得|禁止|不能|不\s*$',re.I)
_ALTERNATIVE=re.compile(r'\bor\b|或者|或是|否则|也可|亦可|或',re.I)
_ALIASES={'secs':'second','sec':'second','seconds':'second','秒':'second','秒钟':'second',
          'ms':'millisecond','milliseconds':'millisecond','毫秒':'millisecond',
          'mins':'minute','min':'minute','minutes':'minute','分钟':'minute',
          'hrs':'hour','hours':'hour','小时':'hour','percent':'%','times':'count','attempts':'count','次':'count'}

def clean_markers(text):
    # Negation inside "not more than" qualifies a bound, not the whole statement.
    return _MARK.sub(' ',text)

def _bounds(text,tokens):
    previous=None
    for marker in _MARK.finditer(text):
        value=_VALUE.match(text,marker.end())
        if not value:continue
        prefix=re.split(r'[,，;；]|\b(?:and|but|whereas)\b',text[:marker.start()])[-1]
        anchors=tokens(clean_markers(prefix))
        if not anchors and previous and re.fullmatch(r'\s*(?:and|且|并且|,|，)\s*',text[previous[0]:marker.start()],re.I):
            anchors=previous[1]
        unit=(value[2] or '').lower();unit=_ALIASES.get(unit,unit.rstrip('s'))
        yield marker.lastgroup,Decimal(value[1]),unit,anchors
        previous=(value.end(),anchors)

def _exact_values(text,tokens):
    for marker in _EXACT.finditer(text):
        value=_VALUE.match(text,marker.end())
        if not value:continue
        prefix=re.split(r'[,，;；]|\b(?:and|but|whereas)\b',text[:marker.start()])[-1]
        if _LIMIT.search(prefix) or _MARK.search(prefix) or _NEGATIVE_PREFIX.search(prefix):continue
        anchors=tokens(clean_markers(prefix))
        unit=(value[2] or '').lower();unit=_ALIASES.get(unit,unit.rstrip('s'))
        yield Decimal(value[1]),unit,anchors

def _same_property(claim,source):
    return bool(claim and source and len(claim&source)>=min(2,len(claim)) and len(claim&source)/len(claim)>=.8)

def threshold_conflict(claim,sentences,tokens):
    sources=[bound for sentence in sentences for bound in _bounds(sentence,tokens)]
    for relation,value,unit,anchors in _bounds(claim,tokens):
        if not anchors:continue
        same=[b for b in sources if b[1:3]==(value,unit) and b[3] and
              len(anchors&b[3])>=min(2,len(anchors)) and len(anchors&b[3])/len(anchors)>=.8]
        # A strict source entails the inclusive form, never the reverse.
        if same and not any(b[0]==relation or (b[0],relation) in {('lt','le'),('gt','ge')} for b in same):
            return 'THRESHOLD_CONTRADICTION'
    exact_sources=[value for sentence in sentences if not _ALTERNATIVE.search(sentence) for value in _exact_values(sentence,tokens)]
    conjunctive_bounds=[b for sentence in sentences if not _ALTERNATIVE.search(sentence) for b in _bounds(sentence,tokens)]
    for value,unit,anchors in _exact_values(claim,tokens):
        related=[b for b in sources if b[2]==unit and _same_property(anchors,b[3])]
        if not related:continue
        # An explicit same-property equality, or matching inclusive upper AND
        # lower bounds, can entail equality. A lone bound cannot.
        if any(v==value and u==unit and _same_property(anchors,a) for v,u,a in exact_sources):continue
        if {'le','ge'}<={b[0] for b in conjunctive_bounds if b[1]==value and b[2]==unit and _same_property(anchors,b[3])}:continue
        return 'THRESHOLD_UNSUPPORTED_EXACT'
    return None
