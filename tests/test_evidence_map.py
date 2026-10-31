import pytest
from techdoc.evidence_map import associate,parts

SOURCES=[{'citation_id':'c1','quotes':['The server timeout is 30 seconds.']},
         {'citation_id':'c2','quotes':['The backup retry limit is 5 attempts.']}]

def test_association_is_lexical_not_an_answer_correctness_claim():
    mapped=associate('What is the server timeout? What is the backup retry limit?',SOURCES)
    assert mapped['contract']=='question_evidence_v1' and not mapped['semantic_coverage_verified']
    assert mapped['parts'][0]['citation_ids']==['c1'] and mapped['parts'][1]['citation_ids']==['c2']
    assert all(not row['semantic_support_verified'] for row in mapped['parts'])

def test_no_citations_or_one_question_does_not_create_empty_ui():
    assert associate('What is the server timeout?',SOURCES) is None
    assert associate('What is A? What is B?',[]) is None

def test_unlinked_does_not_claim_unsupported_and_links_are_bounded():
    sources=[{'citation_id':str(i),'quotes':['The timeout is 30 seconds.']} for i in range(12)]
    result=associate('What is the timeout? What about bananas?',sources)
    assert len(result['parts'][0]['citation_ids'])==4
    assert result['parts'][1]['state']=='unlinked' and not result['parts'][1]['citation_ids']
    assert 'unsupported' not in str(result)

def test_explicit_parts_preserve_overflow_and_ignore_quoted_separators():
    assert parts('Explain `x;y`? Explain "a;b"?')==['Explain `x;y`','Explain "a;b"']
    assert parts('What is 《a;b》?')==[]
    result=parts(';'.join('part'+str(i) for i in range(10)))
    assert len(result)==6 and 'part9' in result[-1]

def test_display_truncation_does_not_drop_tail_matching():
    result=associate('x'*400+' timeout? bananas?',SOURCES)
    assert result['parts'][0]['text_truncated'] and len(result['parts'][0]['question'])==350
    assert result['parts'][0]['citation_ids']==['c1']
