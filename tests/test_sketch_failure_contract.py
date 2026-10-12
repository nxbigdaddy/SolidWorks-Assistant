"""Fault categories are structural host evidence, not localized error strings."""
from __future__ import annotations

import pytest

from sw_assistant.solidworks.gateway import SketchCreationError
from test_circle_repair import Sketch
from test_sketch_creation_coordinates import execute, fixture


def test_verified_empty_creation_is_a_planning_failure_not_a_coordinate_contract_bug():
    gateway, _, manager = fixture("top")
    manager.noop = True
    with pytest.raises(SketchCreationError) as caught:
        execute(gateway, "line")
    evidence = caught.value.evidence
    assert evidence["failure_category"] == "planning"
    assert evidence["contract_failure_code"] == "creation_no_entities"
    assert evidence["actual_geometry"] == [] and evidence["sketch_manager_flags_restored"]


def test_wrong_actual_coordinates_are_adapter_failure():
    gateway, _, manager = fixture("top")
    manager.offset = (.012, 0, 0)
    with pytest.raises(SketchCreationError) as caught:
        execute(gateway, "line")
    assert caught.value.evidence["failure_category"] == "adapter_contract"


@pytest.mark.parametrize("simultaneous_error", [False, True])
def test_configuration_change_remains_safety_even_when_com_also_failed(simultaneous_error):
    gateway, document, manager = fixture("top")
    original = manager.CreateLine

    def mutate(*args):
        entity = original(*args)
        document.ConfigurationManager.ActiveConfiguration.Name = "ChangedConfiguration"
        if simultaneous_error:
            raise RuntimeError("localized irrelevant text")
        return entity

    manager.CreateLine = mutate
    with pytest.raises(SketchCreationError) as caught:
        execute(gateway, "line")
    assert caught.value.evidence["failure_category"] == "safety"
    assert caught.value.evidence["contract_failure_code"] == "configuration_changed"


def test_null_return_after_actual_mutation_is_not_a_retryable_noop():
    gateway, _, manager = fixture("top")
    original = manager.CreateLine

    def mutate(*args):
        original(*args)
        return None

    manager.CreateLine = mutate
    with pytest.raises(SketchCreationError) as caught:
        execute(gateway, "line")
    assert caught.value.evidence["failure_category"] == "adapter_contract"
    assert caught.value.evidence["actual_geometry"]


@pytest.mark.parametrize("simultaneous_error", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_active_sketch_switch_or_close_is_safety_before_binding_validation(closed, simultaneous_error):
    gateway, _, manager = fixture("top")
    original = manager.CreateLine

    def mutate(*args):
        entity = original(*args)
        manager.ActiveSketch = None if closed else Sketch()
        if simultaneous_error:
            raise RuntimeError("localized irrelevant text")
        return entity

    manager.CreateLine = mutate
    with pytest.raises(SketchCreationError) as caught:
        execute(gateway, "line")
    assert caught.value.evidence["failure_category"] == "safety"
    assert caught.value.evidence["contract_failure_code"] == "sketch_changed"
