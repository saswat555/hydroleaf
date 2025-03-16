import asyncio
from asyncio.subprocess import PIPE

async def ping_host(ip: str, timeout: float = 1.0) -> bool:
    # Using Linux-style ping: '-c 1' for one packet, '-W' for timeout in seconds
    proc = await asyncio.create_subprocess_exec(
        "ping", "-c", "1", "-W", str(int(timeout)),
        ip,
        stdout=PIPE, stderr=PIPE
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout + 1)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0
