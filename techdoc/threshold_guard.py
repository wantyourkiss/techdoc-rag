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
_ALIASES={'secs':'second','sec':'second','seconds':'second','秒':'second','秒钟':'second',
          'ms':'millisecond','milliseconds':'millisecond','毫秒':'millisecond',
          'mins':'minute','min':'minute','minutes':'minute','分钟':'minute',
          'hrs':'hour','hours':'hour','小时':'hour','percent':'%','times':'count','attempts':'count','次':'count'}

def clean_markers(text):
    # Negation inside "not more than" qualifies a bound, not the whole statement.
    return _MARK.sub(' ',text)

def _bounds(text,tokens):
    for marker in _MARK.finditer(text):
        value=_VALUE.match(text,marker.end())
        if not value:continue
        prefix=re.split(r'[,，;；]|\b(?:and|but|whereas)\b',text[:marker.start()])[-1]
        anchors=tokens(clean_markers(prefix))
        unit=(value[2] or '').lower();unit=_ALIASES.get(unit,unit.rstrip('s'))
        yield marker.lastgroup,Decimal(value[1]),unit,anchors

def threshold_conflict(claim,sentences,tokens):
    sources=[bound for sentence in sentences for bound in _bounds(sentence,tokens)]
    for relation,value,unit,anchors in _bounds(claim,tokens):
        if not anchors:continue
        same=[b for b in sources if b[1:3]==(value,unit) and b[3] and
              len(anchors&b[3])>=min(2,len(anchors)) and len(anchors&b[3])/len(anchors)>=.8]
        # A strict source entails the inclusive form, never the reverse.
        if same and not any(b[0]==relation or (b[0],relation) in {('lt','le'),('gt','ge')} for b in same):
            return 'THRESHOLD_CONTRADICTION'
    return None
