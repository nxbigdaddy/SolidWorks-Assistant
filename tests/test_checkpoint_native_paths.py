from pathlib import Path

import pytest

from sw_assistant.solidworks.checkpoint_store import CheckpointStore, validate_native_path


def test_real_production_evidence_path_does_not_repeat_uuid_in_filename(tmp_path):
    root = tmp_path / "candidates"
    root.mkdir()
    store = CheckpointStore(root)
    key, path = store.reserve("synthetic-task")
    assert path.name == f"p-{key[:20]}.sldprt"
    assert path.parent.name == key
    assert len(path.parent.parent.name) == 64  # Keep full task hash, no weakened ownership.


@pytest.mark.parametrize("length,allowed", [(254, True), (255, True), (256, False), (268, False)])
def test_native_limit_checked_before_com_independently_of_windows_long_path_policy(length, allowed):
    path = Path("E:/" + "a" * (length - 3))
    if allowed:
        validate_native_path(path)
    else:
        with pytest.raises(ValueError, match="路径过长"):
            validate_native_path(path)


def test_native_path_length_uses_windows_utf16_units():
    path = Path("E:/" + "a" * 251 + "😀")
    assert len(str(path)) == 255
    with pytest.raises(ValueError, match="256 > 255"):
        validate_native_path(path)


def test_too_long_root_refused_before_reserving_checkpoint_directory(tmp_path):
    root = tmp_path / ("x" * 150)
    store = CheckpointStore(root)
    with pytest.raises(ValueError, match="路径过长"):
        store.reserve("synthetic-task")
    assert not root.exists()
