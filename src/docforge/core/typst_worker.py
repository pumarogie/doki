"""Compiles one Typst document in a throwaway process.

Templates are customer code, so they run outside the API process where a timeout can kill them.
stdin: JSON {"source": str, "data": any, "pdfa": bool}. stdout: PDF bytes. Exit 2 on template errors.
"""
import json
import sys
import tempfile

import typst


def main() -> int:
    req = json.load(sys.stdin)
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
            sys.stderr.write(str(e).replace(root, "<template>"))
            return 2
    sys.stdout.buffer.write(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
