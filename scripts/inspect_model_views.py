from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.solidworks.gateway import SolidWorksGateway, _items, _value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    arguments = parser.parse_args()
    source = Path(arguments.path).resolve(strict=True)
    gateway = SolidWorksGateway(visible=True)
    title = ""
    try:
        document, _ = gateway._open_readonly_document(str(source))
        title = str(_value(document, "GetTitle", "") or "")
        print(
            json.dumps(
                {
                    "path": str(source),
                    "title": title,
                    "view_names": [
                        str(value)
                        for value in _items(_value(document, "GetModelViewNames"))
                    ],
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        if title and gateway._sw is not None:
            try:
                gateway._sw.CloseDoc(title)
            except Exception:
                pass
        gateway.release(close_owned=True)


if __name__ == "__main__":
    raise SystemExit(main())
