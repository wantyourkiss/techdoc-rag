import asyncio
from scripts.ipv4_proxy import Bridge,proxy_header


def test_proxy_header_and_opaque_full_duplex_bytes_are_preserved():
    async def exercise():
        headers=[]
        async def target(reader,writer):
            headers.append(await reader.readline())
            data=await reader.read();writer.write(b'reply:'+data);await writer.drain();writer.close();await writer.wait_closed()
        upstream=await asyncio.start_server(target,'127.0.0.1',0)
        bridge=Bridge(port=upstream.sockets[0].getsockname()[1],idle_timeout=2)
        server=await asyncio.start_server(bridge.handle,'127.0.0.1',0)
        try:
            reader,writer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
            writer.write(b'\x16\x03\x01opaque-TLS-bytes');await writer.drain();writer.write_eof()
            assert await asyncio.wait_for(reader.read(),3)==b'reply:\x16\x03\x01opaque-TLS-bytes'
            assert headers[0].startswith(b'PROXY TCP4 127.0.0.1 127.0.0.1 ')
            writer.close();await writer.wait_closed()
        finally:
            server.close();upstream.close();await server.wait_closed();await upstream.wait_closed()
            if bridge.active:await asyncio.wait(tuple(bridge.active),timeout=2)
        assert not bridge.active
    asyncio.run(exercise())
    assert proxy_header(('192.0.2.1',123),('198.51.100.2',18443))==b'PROXY TCP4 192.0.2.1 198.51.100.2 123 18443\r\n'
