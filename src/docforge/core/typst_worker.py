"""Long-lived Typst compiler process.

Templates are customer code, so they run outside the API process where a timeout can kill them.
Protocol on stdin/stdout, one request at a time:
  request:  4-byte big-endian length + JSON {"source": str, "data": any, "pdfa": bool}
  response: 1-byte status (0 ok, 2 template error) + 4-byte length + payload (PDF or error text)
"""
import json
import struct
import sys
import tempfile

import typst

OK, TEMPLATE_ERROR = 0, 2


def compile_one(req: dict) -> tuple[int, bytes]:
    with tempfile.TemporaryDirectory() as root:  # empty root: templates cannot read server files
        try:
            pdf = typst.compile(
                req["source"].encode(),
                root=root,
                sys_inputs={"data": json.dumps(req.get("data", {}))},
                pdf_standards=["a-2b"] if req.get("pdfa") else [],
                ignore_system_fonts=True,
            )
        except typst.TypstError as e:
            return TEMPLATE_ERROR, str(e).replace(root, "<template>").encode()
    return OK, pdf


def _read_exact(stream, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def serve() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        header = _read_exact(stdin, 4)
        if header is None:
            return 0
        body = _read_exact(stdin, struct.unpack(">I", header)[0])
        if body is None:
            return 0
        status, payload = compile_one(json.loads(body))
        stdout.write(struct.pack(">BI", status, len(payload)) + payload)
        stdout.flush()


if __name__ == "__main__":
    sys.exit(serve())
