from pathlib import Path

import pytest

from sw_assistant.domain import ToolError
from sw_assistant.library import DocumentManagerReader


class _Configuration:
    @staticmethod
    def GetCustomPropertyNames():
        return ["Finish"]

    @staticmethod
    def GetCustomProperty2(_name):
        return 30, "Anodized"


class _ConfigurationManager:
    @staticmethod
    def GetConfigurationNames():
        return ["Default"]

    @staticmethod
    def GetConfigurationByName(_name):
        return _Configuration()


class _Document:
    ConfigurationManager = _ConfigurationManager()

    @staticmethod
    def GetCustomPropertyNames():
        return ["PartNumber"]

    @staticmethod
    def GetCustomProperty2(_name):
        return 30, "P-100"

    @staticmethod
    def GetAllExternalReferences4():
        return [r"C:\models\Base.SLDPRT"]


def test_document_manager_manifest_is_explicitly_metadata_only(tmp_path):
    source = tmp_path / "Assembly.SLDASM"
    source.write_bytes(b"assembly")
    manifest = DocumentManagerReader("license")._manifest(_Document(), source, 2)
    assert manifest["reader"] == "document_manager"
    assert manifest["completeness"] == "metadata"
    assert manifest["custom_properties"] == {"PartNumber": "P-100"}
    assert manifest["configuration_properties"] == {
        "Default": {"Finish": "Anodized"}
    }
    assert manifest["references"] == [{"path": r"C:\models\Base.SLDPRT"}]


def test_document_manager_requires_license_before_com_activation(tmp_path):
    source = tmp_path / "Part.SLDPRT"
    source.write_bytes(b"part")
    with pytest.raises(ToolError, match="许可证密钥"):
        DocumentManagerReader().inspect(source)


def test_document_manager_rejects_non_solidworks_file(tmp_path):
    source = tmp_path / "readme.txt"
    source.write_text("x", "utf-8")
    with pytest.raises(ToolError, match="文件类型"):
        DocumentManagerReader("license").inspect(Path(source))
