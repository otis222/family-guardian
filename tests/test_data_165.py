"""165 名單：解析、比對、下載與快取（SPEC 3-2；測試清單第 2 項：命中、未命中、大小寫與子網域）。

全部離線：下載用假的 fetch 函式代替。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from guardian import data_165
from guardian.config import DEFAULT_CONFIG_PATH
from guardian.data_165 import (
    DomainList,
    Source,
    decode_bytes,
    is_stale,
    parse_domains,
    read_meta,
    resolve_download_url,
    update,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone(timedelta(hours=8)))
CSV_TEXT = (
    "網站名稱,網址,件數\n"
    "假平台,https://www.Fake-Invest.example/login,12\n"
    "另一個,bad.example,3\n"
    "重複,https://fake-invest.example,1\n"
)
SOURCE = Source(id="fake_investment", name="假投資網站", dataset_id="160055", expect_title="假投資")
API_URL = "https://data.gov.tw/api/v2/rest/dataset/160055"
CSV_URL = "https://files.example/fake.csv"


def api_payload(title="165反詐騙諮詢專線_假投資(博弈)網站"):
    return json.dumps({"success": True, "result": {"title": title, "distribution": [
        {"resourceFormat": "JSON", "resourceDownloadUrl": "https://files.example/fake.json"},
        {"resourceFormat": "CSV", "resourceDownloadUrl": CSV_URL},
    ]}}, ensure_ascii=False).encode("utf-8")


class FakeFetch:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def good_fetch():
    return FakeFetch({API_URL: api_payload(), CSV_URL: CSV_TEXT.encode("utf-8-sig")})


# ---------------------------------------------------------------- 解析

def test_parse_domains_uses_url_column():
    assert parse_domains(CSV_TEXT) == {"fake-invest.example", "bad.example"}


def test_parse_domains_without_header_scans_every_cell():
    assert parse_domains("a.example\nhttps://b.example/x,備註\n") == {"a.example", "b.example"}


def test_parse_domains_skips_values_that_are_not_domains():
    assert parse_domains("網址\n123.45\n某某平台\n2024/01/02\n") == set()


def test_decode_handles_bom_and_big5():
    assert decode_bytes("﻿網址\na.example".encode("utf-8")) == "網址\na.example"
    assert decode_bytes("網址\na.example".encode("cp950")) == "網址\na.example"


# ---------------------------------------------------------------- 比對

def test_domain_list_matching():
    blocklist = DomainList({"bad.example": "s1", "www.evil.example": "s2", "a.sub.example": "s3"})
    assert blocklist.match("https://bad.example/x") == "s1"  # 命中
    assert blocklist.match("HTTPS://BAD.EXAMPLE") == "s1"  # 大小寫
    assert blocklist.match("https://login.bad.example") == "s1"  # 子網域
    assert blocklist.match("http://evil.example") == "s2"  # www. 視同沒有
    assert blocklist.match("https://sub.example") is None  # 名單是子網域時，不涵蓋上層
    assert blocklist.match("https://notbad.example") is None  # 只是字尾相同不算
    assert blocklist.match("https://good.example") is None  # 未命中


# ---------------------------------------------------------------- 下載與快取

def test_update_downloads_and_caches(tmp_path):
    results = update(tmp_path, [SOURCE], fetch=good_fetch(), now=NOW)
    assert [(r.status, r.domains) for r in results] == [("updated", 2)]
    blocklist = DomainList.from_cache(tmp_path, [SOURCE])
    assert blocklist.match("https://bad.example") == "fake_investment"
    assert read_meta(tmp_path)["fake_investment"]["download_url"] == CSV_URL


def test_fresh_cache_is_not_downloaded_again(tmp_path):
    update(tmp_path, [SOURCE], fetch=good_fetch(), now=NOW)
    fetch = good_fetch()
    assert update(tmp_path, [SOURCE], fetch=fetch, now=NOW + timedelta(days=1))[0].status == "fresh"
    assert fetch.calls == []
    assert update(tmp_path, [SOURCE], fetch=fetch, now=NOW + timedelta(days=7))[0].status == "updated"


def test_title_mismatch_is_skipped(tmp_path):
    fetch = FakeFetch({API_URL: api_payload(title="某某停車場即時資訊")})
    result = update(tmp_path, [SOURCE], fetch=fetch, now=NOW)[0]
    assert result.status == "failed" and "已跳過" in result.message
    assert not (tmp_path / "fake_investment.csv").exists()
    assert CSV_URL not in fetch.calls


def test_failure_keeps_old_cache_and_does_not_retry(tmp_path):
    update(tmp_path, [SOURCE], fetch=good_fetch(), now=NOW)
    broken = FakeFetch({API_URL: api_payload(), CSV_URL: OSError("連線逾時")})
    result = update(tmp_path, [SOURCE], fetch=broken, now=NOW + timedelta(days=8))[0]
    assert result.status == "failed" and result.domains == 2
    assert broken.calls.count(CSV_URL) == 1  # 不重試
    assert DomainList.from_cache(tmp_path, [SOURCE]).match("bad.example") == "fake_investment"


def test_empty_download_counts_as_failure(tmp_path):
    fetch = FakeFetch({API_URL: api_payload(), CSV_URL: "網站名稱,網址\n".encode("utf-8")})
    assert update(tmp_path, [SOURCE], fetch=fetch, now=NOW)[0].status == "failed"


def test_direct_download_url_skips_the_api(tmp_path):
    source = Source(id="direct", name="直接下載", download_url=CSV_URL)
    fetch = FakeFetch({CSV_URL: CSV_TEXT.encode("utf-8")})
    assert update(tmp_path, [source], fetch=fetch, now=NOW)[0].status == "updated"
    assert fetch.calls == [CSV_URL]


def test_resolve_download_url_picks_the_csv():
    assert resolve_download_url(SOURCE, FakeFetch({API_URL: api_payload()})) == CSV_URL


def test_is_stale():
    assert is_stale(None, NOW, 7)
    assert not is_stale({"downloaded_at": NOW.isoformat()}, NOW + timedelta(days=6), 7)
    assert is_stale({"downloaded_at": NOW.isoformat()}, NOW + timedelta(days=7), 7)
    assert is_stale({"downloaded_at": "不是時間"}, NOW, 7)


def test_cli_status_without_cache(tmp_path, capsys):
    config = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    config["data_165"]["cache_dir"] = str(tmp_path / "cache")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    assert data_165.main(["--config", str(path)]) == 0
    assert "尚未下載" in capsys.readouterr().out


@pytest.mark.parametrize("title", [None, "其他資料集"])
def test_missing_or_wrong_title_raises(title):
    payload = json.dumps({"result": {"title": title, "distribution": []}}).encode("utf-8")
    with pytest.raises(data_165.Data165Error):
        resolve_download_url(SOURCE, FakeFetch({API_URL: payload}))
