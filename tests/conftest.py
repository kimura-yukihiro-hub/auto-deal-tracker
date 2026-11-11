"""テスト共通の部品。ネットワークにはアクセスしない。"""

from __future__ import annotations

from datetime import datetime

import pytest

import fetch_and_build as fb


@pytest.fixture
def make_item():
    def _make(title, link="https://example.com/a", category="製品ニュース", source="PC Watch", hour=12, summary="要約"):
        return fb.Item(title, summary, link, datetime(2026, 11, 11, hour, tzinfo=fb.JST), source, category)

    return _make


@pytest.fixture(autouse=True)
def _no_github_actions(monkeypatch):
    """GitHub Actions 上でもテスト中は注釈を出さない。"""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
