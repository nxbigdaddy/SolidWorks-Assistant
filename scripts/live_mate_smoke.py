from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.solidworks.gateway import SolidWorksGateway, _items, _value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _planar_face(component):
    for body in _items(component.GetBodies2(0)):
        for face in _items(_value(body, "GetFaces")):
            surface = _value(face, "GetSurface")
            if surface is not None and bool(_value(surface, "IsPlane", False)):
                return face
    raise RuntimeError("测试组件没有可用平面。")


def _reference(gateway: SolidWorksGateway, document, entity) -> str:
    document.ClearSelection2(True)
    if not bool(entity.Select4(False, gateway._null_dispatch())):
        raise RuntimeError("SolidWorks 无法在装配体上下文中选择测试平面。")
    selected = document.SelectionManager.GetSelectedObject6(1, -1)
    value = document.Extension.GetPersistReference3(selected)
    payload = SolidWorksGateway._reference_bytes(value)
    if not payload:
        selection_type = document.SelectionManager.GetSelectedObjectType3(1, -1)
        raise RuntimeError(
            "SolidWorks 没有返回装配体实体持久引用："
            f"selection_type={selection_type}, raw_type={type(value).__name__}。"
        )
    return base64.b64encode(payload).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("assembly")
    parser.add_argument("--mate-type", default="parallel")
    arguments = parser.parse_args()
    source = Path(arguments.assembly).resolve(strict=True)
    before_hash = _sha256(source)
    gateway = SolidWorksGateway(visible=True)
    title = ""
    try:
        document, _ = gateway._open_document(str(source), readonly=False)
        title = str(_value(document, "GetTitle", "") or "")
        assembly = gateway._cast_document(document, "AssemblyDoc")
        components = _items(assembly.GetComponents(False))
        if len(components) < 2:
            raise RuntimeError("测试装配体至少需要两个组件。")
        references = [
            _reference(gateway, document, _planar_face(component))
            for component in components[:2]
        ]
        result = gateway._create_assembly_mate(
            {
                "assembly_path": str(source),
                "first_reference": references[0],
                "second_reference": references[1],
                "mate_type": arguments.mate_type,
                "alignment": "closest",
                "value": 0.0,
            }
        )
        if not result.get("created") or not result.get("manual_confirmation_required"):
            raise RuntimeError("配合创建结果缺少创建或人工确认标志。")
        document.EditUndo2(1)
        gateway._sw.CloseDoc(title)
        title = ""
    finally:
        if title and gateway._sw is not None:
            try:
                gateway._sw.CloseDoc(title)
            except Exception:
                pass
        gateway.release(close_owned=True)
    after_hash = _sha256(source)
    if after_hash != before_hash:
        raise RuntimeError("未保存的配合测试改变了源装配体文件。")
    print(
        json.dumps(
            {
                "ok": True,
                "assembly": str(source),
                "source_sha256": after_hash,
                "mate": result,
                "discarded_after_verification": True,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
