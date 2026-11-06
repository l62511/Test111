import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from data.seed import seed  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个测试一份独立的数据目录，互不干扰。"""
    directory = seed(tmp_path / "data")
    monkeypatch.setenv("ORDER_DATA_DIR", str(directory))
    return directory
