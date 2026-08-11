from __future__ import annotations

import copy

import pytest

from rep_patch.errors import PackageValidationError
from rep_patch.security import safe_relative_path, sign_document, verify_document


def test_hmac_signature_detects_changes() -> None:
    value = sign_document({"schema_version": 1, "packages": []}, "test-password")
    verify_document(value, "test-password")
    modified = copy.deepcopy(value)
    modified["packages"].append({"repo_id": "tampered"})
    with pytest.raises(PackageValidationError):
        verify_document(modified, "test-password")


@pytest.mark.parametrize("value", ["../secret", "/absolute", "C:/Windows/file", "safe/../../bad"])
def test_unsafe_relative_paths_are_rejected(value: str) -> None:
    with pytest.raises(PackageValidationError):
        safe_relative_path(value)


def test_normal_relative_path_is_allowed() -> None:
    assert str(safe_relative_path("packages/app/000001/manifest.json")) == "packages/app/000001/manifest.json"
