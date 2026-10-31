"""Safe provider status vocabulary. Never returns exception messages or bodies."""
KINDS=frozenset(('quota','rate_limit','network','authentication','access_denied','unavailable','invalid_response','unknown'))
MESSAGES={
 'quota':('模型用量已达上限','大模型服务的用量已达上限。可待额度恢复后重试，或选择只查原文。'),
 'rate_limit':('模型调用受限','大模型服务暂时限制调用频率。请稍后重试，或选择只查原文。'),
 'network':('模型连接未完成','大模型服务连接超时或中断。可稍后重试，或选择只查原文。'),
 'authentication':('模型认证未通过','大模型服务认证未通过，请检查后端密钥配置；也可选择只查原文。'),
 'access_denied':('模型访问受限','大模型服务拒绝访问，请检查后端配置和服务权限；也可选择只查原文。'),
 'unavailable':('模型服务暂不可用','大模型服务暂不可用。可稍后重试，或选择只查原文。'),
 'invalid_response':('模型回复未通过检查','大模型返回的内容未通过格式或完整性检查。可重试，或选择只查原文。'),
 'unknown':('模型处理未完成','大模型处理未完成。可稍后重试，或选择只查原文。'),
}

def category_kind(category):
    if category=='PROVIDER_HTTP_429':return 'rate_limit'
    if category=='PROVIDER_HTTP_401':return 'authentication'
    if category=='PROVIDER_HTTP_403':return 'access_denied'
    if category=='PROVIDER_NETWORK_OR_TIMEOUT':return 'network'
    if category in ('PROVIDER_HTTP_500','PROVIDER_HTTP_502','PROVIDER_HTTP_503','PROVIDER_HTTP_504'):return 'unavailable'
    if category in ('MALFORMED_PROVIDER_RESPONSE','OVERSIZED_PROVIDER_RESPONSE','TRUNCATED_GENERATION','NON_FINAL_GENERATION','EMPTY_MODEL_CONTENT'):return 'invalid_response'
    return 'unknown'

def public_failure(exc,stage):
    category=getattr(exc,'category',None)
    kind=getattr(exc,'failure_kind',None)
    if not isinstance(kind,str) or kind not in KINDS:kind=category_kind(category)
    return {'kind':kind,'stage':stage if stage in ('generation','support_review','claim_review') else 'generation',
            'retryable':kind in ('rate_limit','network','unavailable'),'contract':'provider_status_v1'}

def failure_message(failure):return MESSAGES.get(failure.get('kind'),MESSAGES['unknown'])[1]
