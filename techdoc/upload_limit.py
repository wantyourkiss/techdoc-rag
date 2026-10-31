"""Bound multipart spooling, including uploads without a Content-Length header."""
from .core import MAX_BYTES
from .knowledge_api import error_response
from starlette.formparsers import MultiPartException


class UploadBodyTooLarge(MultiPartException):
    pass


class KnowledgeUploadLimit:
    def __init__(self,app): self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method'] not in ('POST','PATCH') or not scope['path'].startswith('/api/v1/'):
            return await self.app(scope,receive,send)
        limit=MAX_BYTES+64*1024; size=0; exceeded=False; replaced=False
        from starlette.requests import Request
        request=Request(scope)
        async def too_large():
            response=error_response(request,'EMPTY_OR_OVERSIZED_UPLOAD',413)
            await response(scope,receive,send)
        try: declared=int(request.headers.get('content-length','0'))
        except ValueError: declared=0
        if declared>limit: return await too_large()
        async def bounded_receive():
            nonlocal size,exceeded
            message=await receive()
            size+=len(message.get('body',b''))
            if size>limit:
                exceeded=True
                raise UploadBodyTooLarge('Upload body exceeds limit')
            return message
        async def bounded_send(message):
            nonlocal replaced
            # Multipart parsing can translate receive exceptions to HTTP 400.
            if exceeded:
                if not replaced:
                    replaced=True
                    await too_large()
                return
            await send(message)
        try:
            await self.app(scope,bounded_receive,bounded_send)
        except UploadBodyTooLarge:
            if not replaced: await too_large()
