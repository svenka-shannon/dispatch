#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Upload N bytes to speed.cloudflare.com/__up with an explicit SO_SNDBUF (bypasses autosndbufmax cap)."""
import socket, ssl, sys, time
import os; host=os.environ.get("H","speed.cloudflare.com"); path=os.environ.get("P","/__up")
def run(sndbuf, nbytes=2*1024*1024, timeout=30):
    fam=socket.AF_INET if os.environ.get("FAM")=="4" else socket.AF_INET6; addr=socket.getaddrinfo(host,443,fam,socket.SOCK_STREAM)[0][4]; s=socket.socket(fam, socket.SOCK_STREAM)
    if sndbuf: s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
    s.settimeout(timeout)
    t0=time.time()
    try:
        s.connect(addr)
        eff=s.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        ts=ssl.create_default_context().wrap_socket(s, server_hostname=host)
        ts.sendall(f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/octet-stream\r\nContent-Length: {nbytes}\r\nConnection: close\r\n\r\n".encode())
        chunk=b"\0"*65536; sent=0
        while sent<nbytes:
            n=min(len(chunk),nbytes-sent); ts.sendall(chunk[:n]); sent+=n
        resp=ts.recv(64).split(b"\r\n")[0].decode(errors="ignore")
        ts.close()
        return True, time.time()-t0, eff, resp
    except Exception as e:
        return False, time.time()-t0, None, f"{type(e).__name__}: {e}"
if __name__=="__main__":
    sndbuf=int(sys.argv[1]); n=int(sys.argv[2]) if len(sys.argv)>2 else 10
    ok=0
    for i in range(n):
        r=run(sndbuf); ok+=r[0]
        print(f"sndbuf={sndbuf} run {i+1}: ok={r[0]} {r[1]:.1f}s eff={r[2]} {r[3]}", flush=True)
    print(f"RESULT sndbuf={sndbuf}: {ok}/{n} passed")
