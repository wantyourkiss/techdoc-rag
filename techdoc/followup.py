"""Bounded user-topic context, separate from the current question and evidence."""
from dataclasses import dataclass
import re

RECIPE='separate_v1'
FUNCTION_WORDS=frozenset('what which who when where why how is are was were do does did may can could should would have has had the a an in of to by and or for with from on at this that it its be as about please explain describe'.split())
QUESTION_WORDS=re.compile(r'为什么|是什么|什么是|是不是|哪些|什么|怎么|如何|为何|是否|多少|怎样|请问|请解释|请说明')


def needs_history(question):
    """A bounded routing heuristic, not semantic validation of the question.

    Continuation words inside a complete sentence are ordinary query text. An
    explicit topic before a pronoun supplies its context in the current turn.
    Bare references still need a successful, authorized previous turn.
    """
    text=question.strip().strip('。！？!?.,，；;：: ')
    if re.match(r'^(?:请|麻烦)?(?:继续|接着)(?:上次|刚才|之前|前面|上一)|^(?:please\s+)?continue\s+(?:the\s+)?previous\b',text,re.I):return True
    if re.fullmatch(r'(?:请|麻烦)?(?:继续|接着)(?:说|讲|讲解|解释|说明|介绍|回答|展开)?(?:一下|下去|上一个问题)?|然后呢?|再(?:详细)?说(?:一点|一些)|(?:please\s+)?(?:continue|go on|what next)(?:\s+please)?',text,re.I):return True
    marker=re.search(r'这个|这种|那种|上述|上面|(?<!其)它(?:们)?|\b(?:this|that|it|these|those)\b',text,re.I)
    if not marker:return False
    # A named technical subject can follow a demonstrative: "这个BGE-M3模型"
    # or "this RAG pipeline". An attribute after "it" is not its antecedent.
    suffix=text[marker.end():]
    if marker.group().lower() in ('这个','这种','那种','this','that','these','those') and re.match(r'\s*(?:模型|系统|算法)?[A-Z][A-Za-z0-9_.-]+',suffix):return False
    prefix=QUESTION_WORDS.sub(' ',text[:marker.start()])
    prefix=re.sub(r'请|帮我|告诉我|给我|帮忙|能否|可以|详细|具体|解释|讲解|讲讲|说明|比较|分析|介绍|一下|之间|区别|分别',' ',prefix)
    chinese=re.findall(r'[\u4e00-\u9fff]+',prefix)
    english=re.findall(r'[A-Za-z][A-Za-z0-9_.-]*',prefix)
    filler=FUNCTION_WORDS|{'compare','difference','differences','between','tell','me','more','detail','detailed','these','those','you','we','i','us','your','our'}
    has_subject=any(len(s.strip('的了呢吗么是在为与和及把将其请要'))>=2 for s in chinese) or any(s.lower() not in filler for s in english)
    return not has_subject


def topic_words(previous_question):
    """No model rewriting and no previous answer; at most six short topic terms."""
    previous_question=previous_question[:500]
    english=[m.group() for m in re.finditer(r'[A-Za-z][A-Za-z0-9_.-]*',previous_question)
             if m.group().lower() not in FUNCTION_WORDS]
    chinese=re.findall(r'[\u4e00-\u9fff]+',QUESTION_WORDS.sub(' ',previous_question))
    chinese=[s.strip('的了呢吗么是在为与和及把将其请要')[:24] for s in chinese]
    terms=list(dict.fromkeys(english+[s for s in chinese if len(s)>=2]))[:6]
    return ' '.join(terms)[:96]


@dataclass(frozen=True)
class FollowupContext:
    previous_user_question: str
    retrieval_topic: str

    @classmethod
    def from_dict(cls,value):
        if value is None:return None
        if (not isinstance(value,dict) or set(value)!={'recipe','previous_user_question','retrieval_topic'}
                or value['recipe']!=RECIPE or not isinstance(value['previous_user_question'],str)
                or not 1<=len(value['previous_user_question'])<=500
                or not isinstance(value['retrieval_topic'],str) or len(value['retrieval_topic'])>96
                or value['retrieval_topic']!=topic_words(value['previous_user_question'])):
            raise ValueError('INVALID_FOLLOWUP_CONTEXT')
        return cls(value['previous_user_question'],value['retrieval_topic'])

    def public(self):return {'recipe':RECIPE,'previous_user_question':self.previous_user_question,'retrieval_topic':self.retrieval_topic}
