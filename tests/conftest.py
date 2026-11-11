import os

import pytest


@pytest.fixture(autouse=True)
def _clear_aw_profile_after_test():
    """export_profile() writes os.environ directly; monkeypatch.delenv does
    not record an undo when the var was already unset, so later tests would
    inherit a leftover profile."""
    yield
    os.environ.pop("AW_PROFILE", None)


@pytest.fixture(autouse=True)
def _config_dir_in_tmp(tmp_path, monkeypatch):
    """The loop writes its status file in the config folder; tests must not touch the real one."""
    from aw_client import timelog_module

    monkeypatch.setattr(timelog_module.dirs, "get_config_dir", lambda name=None: str(tmp_path))
