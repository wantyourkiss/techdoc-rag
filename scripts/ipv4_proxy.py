"""Bounded systemd TCP bridge: preserve source via PROXY v1, keep TLS end-to-end.

Backend must explicitly trust this private loopback hop's PROXY headers. This
program never reads TLS keys, decrypts HTTP, or binds an extra management port.
"""
import asyncio,ipaddress,os,signal,socket


def proxy_header(peer,local):
    source=ipaddress.ip_address(peer[0]);destination=ipaddress.ip_address(local[0])
    if source.version!=4 or destination.version!=4:raise ValueError('IPV4_PROXY_ADDRESS_REQUIRED')
    return f'PROXY TCP4 {source} {destination} {int(peer[1])} {int(local[1])}\r\n'.encode('ascii')


class Bridge:
    def __init__(self,host='127.0.0.1',port=18765,capacity=64,idle_timeout=300):
        self.host=host;self.port=port;self.capacity=capacity;self.idle_timeout=idle_timeout;self.active=set()

    async def handle(self,reader,writer):
        task=asyncio.current_task();upstream=None
        if len(self.active)>=self.capacity:
            writer.close();await writer.wait_closed();return
        self.active.add(task)
        async def pump(source,destination):
            while True:
                data=await asyncio.wait_for(source.read(65536),self.idle_timeout)
                if not data:
                    if destination.can_write_eof():destination.write_eof()
                    return
                destination.write(data);await asyncio.wait_for(destination.drain(),self.idle_timeout)
        tasks=[]
        try:
            backend,upstream=await asyncio.wait_for(asyncio.open_connection(self.host,self.port),10)
            upstream.write(proxy_header(writer.get_extra_info('peername'),writer.get_extra_info('sockname')))
            await upstream.drain()
            tasks=[asyncio.create_task(pump(reader,upstream)),asyncio.create_task(pump(backend,writer))]
            await asyncio.gather(*tasks)
        except (OSError,ValueError,TimeoutError,ConnectionError):pass
        finally:
            for item in tasks:
                if not item.done():item.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            for stream in (upstream,writer):
                if stream:
                    stream.close()
                    try:await stream.wait_closed()
                    except (OSError,ConnectionError):pass
            self.active.discard(task)


async def main():
    if os.getenv('LISTEN_PID')!=str(os.getpid()) or os.getenv('LISTEN_FDS')!='1':
        raise RuntimeError('ONE_SYSTEMD_LISTEN_SOCKET_REQUIRED')
    listener=socket.socket(fileno=3)
    if listener.family!=socket.AF_INET or listener.type&socket.SOCK_STREAM!=socket.SOCK_STREAM:
        raise RuntimeError('IPV4_STREAM_SOCKET_REQUIRED')
    listener.setblocking(False);bridge=Bridge();stopped=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stopped.set)
    server=await asyncio.start_server(bridge.handle,sock=listener)
    try:await stopped.wait()
    finally:
        server.close();await server.wait_closed()
        if bridge.active:
            _,pending=await asyncio.wait(tuple(bridge.active),timeout=15)
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)


if __name__=='__main__':asyncio.run(main())
