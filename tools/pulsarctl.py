#!/usr/bin/env python3
"""pulsarctl - send one command to pulsard and print the JSON reply.

  pulsarctl.py status
  pulsarctl.py catalog
  pulsarctl.py load CSineR4.dsp [dsp=2] [name=Sine]
  pulsarctl.py connect src=n9 out=0 dst=n4 in=0
  pulsarctl.py set id=n9 in=0 value=0x05555555
  pulsarctl.py disconnect dst=n4 in=0
  pulsarctl.py unload id=n9
"""
import json
import os
import socket
import sys

SOCK = os.environ.get("PULSARD_SOCKET", "/run/pulsard.sock")


def request(req, sock=SOCK):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    req = {"cmd": argv[0]}
    for a in argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            req[k] = int(v, 0) if v.lstrip("-").replace("x", "").isalnum() and v[:1].isdigit() else v
        else:
            req["file"] = a
    res = request(req)
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
