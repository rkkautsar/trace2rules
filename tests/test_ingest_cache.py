from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trace2rules.ingest.normalize import _cached_source, _remote_url


class FakeResponse:
    def __init__(self, body: bytes, length: int | None = None):
        self.body = body
        self.headers = {"Content-Length": str(length if length is not None else len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int) -> bytes:
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


def _cache_path(tmp_path: Path, url: str) -> Path:
    return tmp_path / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.parquet"


def test_remote_url_mapping():
    assert _remote_url("hf://datasets/o/d/p.parquet") == (
        "https://huggingface.co/datasets/o/d/resolve/main/p.parquet"
    )
    assert _remote_url("https://example.com/a.parquet") == "https://example.com/a.parquet"
    assert _remote_url("data/local.parquet") is None
    assert _remote_url("hf://datasets/o/d") is None


def test_cache_hit_skips_network(tmp_path, monkeypatch):
    url = "https://example.com/data.parquet"
    path = _cache_path(tmp_path, url)
    path.write_bytes(b"PAR1payloadPAR1")
    monkeypatch.setenv("TRACE2RULES_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network"))
    assert _cached_source(url) == str(path)


def test_truncated_download_is_rejected(tmp_path, monkeypatch):
    url = "https://example.com/data.parquet"
    monkeypatch.setenv("TRACE2RULES_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: FakeResponse(b"PAR1", length=100),
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        _cached_source(url)
    assert not _cache_path(tmp_path, url).exists()
    assert not list(tmp_path.glob("*.part"))


def test_invalid_parquet_is_rejected(tmp_path, monkeypatch):
    url = "https://example.com/data.parquet"
    monkeypatch.setenv("TRACE2RULES_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: FakeResponse(b"<html>not parquet</html>"),
    )
    with pytest.raises(RuntimeError, match="not a parquet"):
        _cached_source(url)
    assert not _cache_path(tmp_path, url).exists()


def test_refresh_redownloads(tmp_path, monkeypatch):
    url = "https://example.com/data.parquet"
    path = _cache_path(tmp_path, url)
    path.write_bytes(b"PAR1oldPAR1")
    monkeypatch.setenv("TRACE2RULES_CACHE_DIR", str(tmp_path))
    fresh = b"PAR1newPAR1"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: FakeResponse(fresh),
    )
    assert _cached_source(url, refresh=True) == str(path)
    assert path.read_bytes() == fresh
