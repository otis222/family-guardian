"""165 開放資料：下載、快取、網域比對（SPEC 3-2，決策 D7）。

- 來源寫在 config.yaml 的 data_165.sources。
- 下載：用 data.gov.tw 的資料集 API 找 CSV 下載網址，並先比對資料集標題，避免抓錯資料集；
  來源有填 download_url 時直接用它。
- 快取：data/165/<來源 id>.csv（只有 domain 一欄），另存 meta.json（下載時間、網域數、下載網址）。
- 下載失敗沿用舊快取，不重試、不擋啟動。
- 闢謠專區（kind=rumors）另存 title、content 兩欄，給查證 Worker 的 search_165_rumors 用（RumorIndex）。

在自己的電腦上執行（每週一次）：
    python -m guardian.data_165 --update
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .fileio import write_text_atomic
from .urls import domain_suffixes, normalize_width, url_host

DATASET_API = "https://data.gov.tw/api/v2/rest/dataset/{dataset_id}"
USER_AGENT = "family-guardian/0.2 (TAICA course project)"
MAX_BYTES = 20 * 1024 * 1024
TAIPEI = timezone(timedelta(hours=8))
URL_COLUMN_HINTS = ("網址", "url", "網域", "domain")
META_FILE = "meta.json"
MAX_RUMOR_CHARS = 2000
SNIPPET_CHARS = 80  # 放回 prompt 的摘錄長度，控制 token
MIN_SHARED_GRAMS = 3

Fetcher = Callable[[str], bytes]


class Data165Error(RuntimeError):
    """165 資料下載或解析失敗（訊息可以直接顯示）。"""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    dataset_id: str | None = None
    download_url: str | None = None
    expect_title: str | None = None
    kind: str = "domains"  # domains｜rumors


@dataclass(frozen=True)
class UpdateResult:
    source_id: str
    status: str  # updated｜fresh｜failed
    domains: int
    message: str = ""


# ---------------------------------------------------------------- 解析

def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Data165Error("無法辨識檔案編碼（不是 UTF-8，也不是 Big5）")


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _cell_hosts(cell: str) -> Iterator[str]:
    for token in re.split(r"[\s、，,；;|]+", cell.strip().strip("\"'")):
        if "." in token:
            host = url_host(token)
            if host:
                yield _strip_www(host)


def parse_domains(csv_text: str) -> set[str]:
    """從 CSV 取出網域。有「網址／URL／網域／domain」欄位就只看那幾欄，沒有就每一格都試。"""
    rows = [row for row in csv.reader(io.StringIO(csv_text)) if any(cell.strip() for cell in row)]
    if not rows:
        return set()
    header = [cell.strip().lower() for cell in rows[0]]
    url_columns = [index for index, name in enumerate(header) if any(hint in name for hint in URL_COLUMN_HINTS)]
    domains: set[str] = set()
    for row in rows[1:] if url_columns else rows:
        cells = [row[index] for index in url_columns if index < len(row)] if url_columns else row
        for cell in cells:
            domains.update(_cell_hosts(cell))
    return domains


_TAG = re.compile(r"<[^>]+>")


def _clean_cell(cell: str | None) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", cell or "")).split())


def parse_rumors(csv_text: str) -> list[tuple[str, str]]:
    """闢謠專區的 CSV（編號、標題、發佈時間、發佈內容）→ (標題, 內容)。"""
    reader = csv.DictReader(io.StringIO(csv_text))
    fields = reader.fieldnames or []
    title_key = next((name for name in fields if "標題" in name), None)
    content_key = next((name for name in fields if "內容" in name), None)
    if not title_key or not content_key:
        return []
    rows: dict[str, str] = {}
    for row in reader:
        title = _clean_cell(row.get(title_key))
        if title:
            rows.setdefault(title, _clean_cell(row.get(content_key))[:MAX_RUMOR_CHARS])
    return list(rows.items())


def _rumor_csv(rows: list[tuple[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["title", "content"])
    writer.writerows(rows)
    return buffer.getvalue()


# ---------------------------------------------------------------- data.gov.tw API

def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _find_title(payload: Any) -> str | None:
    result = payload.get("result") if isinstance(payload, dict) else None
    for candidate in (result, payload):
        if isinstance(candidate, dict):
            for key in ("title", "datasetTitle", "name"):
                if isinstance(candidate.get(key), str):
                    return candidate[key]
    return None


def _find_csv_url(payload: Any) -> str | None:
    others: list[str] = []
    for node in _walk(payload):
        url = next((value for key, value in node.items()
                    if isinstance(value, str) and "downloadurl" in key.lower().replace("_", "")), None)
        if not url or not url.lower().startswith(("http://", "https://")):
            continue
        formats = " ".join(str(value) for key, value in node.items() if "format" in key.lower())
        if "csv" in formats.lower() or url.lower().split("?")[0].endswith(".csv"):
            return url
        others.append(url)
    return others[0] if len(others) == 1 else None


def resolve_download_url(source: Source, fetch: Fetcher) -> str:
    if source.download_url:
        return source.download_url
    if not source.dataset_id:
        raise Data165Error(f"{source.name}：沒有 dataset_id，也沒有 download_url")
    try:
        payload = json.loads(decode_bytes(fetch(DATASET_API.format(dataset_id=source.dataset_id))))
    except (json.JSONDecodeError, Data165Error):
        raise Data165Error(f"{source.name}：資料集 API 回傳的不是 JSON") from None
    title = _find_title(payload)
    if source.expect_title and (not title or source.expect_title not in title):
        raise Data165Error(
            f"{source.name}：資料集 {source.dataset_id} 的標題是「{title or '找不到'}」，"
            f"不含「{source.expect_title}」，已跳過"
        )
    url = _find_csv_url(payload)
    if not url:
        raise Data165Error(f"{source.name}：資料集 API 裡找不到 CSV 下載網址")
    return url


def http_get(url: str, timeout: float = 30.0) -> bytes:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310（只抓設定檔裡的網址）
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Data165Error("下載的檔案超過 20 MB，已停止")
    return data


# ---------------------------------------------------------------- 快取

def read_meta(cache_dir: Path) -> dict[str, Any]:
    path = Path(cache_dir) / META_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def is_stale(entry: Any, now: datetime, max_age_days: int) -> bool:
    if not isinstance(entry, dict) or "downloaded_at" not in entry:
        return True
    try:
        downloaded = datetime.fromisoformat(entry["downloaded_at"])
    except (TypeError, ValueError):
        return True
    return now - downloaded >= timedelta(days=max_age_days)


def update(
    cache_dir: Path,
    sources: Iterable[Source],
    *,
    fetch: Fetcher = http_get,
    now: datetime | None = None,
    max_age_days: int = 7,
    force: bool = False,
) -> list[UpdateResult]:
    """下載過期的名單。任何一個來源失敗都只影響它自己，並沿用它的舊快取。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(TAIPEI)
    meta = read_meta(cache_dir)
    results: list[UpdateResult] = []
    for source in sources:
        entry = meta.get(source.id) if isinstance(meta.get(source.id), dict) else {}
        old_count = int(entry.get("domains", 0) or 0)
        if not force and not is_stale(entry, now, max_age_days):
            results.append(UpdateResult(source.id, "fresh", old_count, "還在有效期限內"))
            continue
        try:
            url = resolve_download_url(source, fetch)
            text = decode_bytes(fetch(url))
            if source.kind == "rumors":
                rumors = parse_rumors(text)
                count, content = len(rumors), _rumor_csv(rumors)
            else:
                domains = parse_domains(text)
                count, content = len(domains), "\n".join(["domain", *sorted(domains)]) + "\n"
            if not count:
                raise Data165Error(f"{source.name}：下載的檔案裡找不到任何{'文章' if source.kind == 'rumors' else '網域'}")
        except Data165Error as exc:
            results.append(UpdateResult(source.id, "failed", old_count, str(exc)))
            continue
        except Exception as exc:  # 網路錯誤等：記下來、沿用舊快取，不重試
            message = f"{source.name}：{type(exc).__name__}: {exc}"
            results.append(UpdateResult(source.id, "failed", old_count, message[:300]))
            continue
        write_text_atomic(cache_dir / f"{source.id}.csv", content)
        meta[source.id] = {
            "name": source.name,
            "dataset_id": source.dataset_id,
            "download_url": url,
            "downloaded_at": now.isoformat(timespec="seconds"),
            "domains": count,
        }
        results.append(UpdateResult(source.id, "updated", count))
    write_text_atomic(cache_dir / META_FILE, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return results


# ---------------------------------------------------------------- 比對

class DomainList:
    """165 名單：名單上的網域也涵蓋子網域，比對不分大小寫，www. 視同沒有。"""

    def __init__(self, domains: dict[str, str] | None = None, meta: dict[str, Any] | None = None) -> None:
        self._domains = {_strip_www(domain.lower()): source for domain, source in (domains or {}).items()}
        self.meta = meta or {}

    def __len__(self) -> int:
        return len(self._domains)

    def match(self, url_or_host: str) -> str | None:
        """命中時回傳來源 id，沒命中回傳 None。"""
        host = url_host(url_or_host)
        if not host:
            return None
        for candidate in domain_suffixes(host):
            if candidate in self._domains:
                return self._domains[candidate]
        return None

    @classmethod
    def from_cache(cls, cache_dir: Path, sources: Iterable[Source]) -> DomainList:
        cache_dir = Path(cache_dir)
        domains: dict[str, str] = {}
        for source in sources:
            path = cache_dir / f"{source.id}.csv"
            if not path.is_file():
                continue
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    domain = (row.get("domain") or "").strip().lower()
                    if domain:
                        domains.setdefault(domain, source.id)
        return cls(domains, read_meta(cache_dir))

    def describe(self) -> str:
        if not self._domains:
            return "尚未下載（在自己電腦上執行 python -m guardian.data_165 --update）"
        dates = sorted(entry.get("downloaded_at", "")[:10] for entry in self.meta.values() if isinstance(entry, dict))
        newest = f"，最近更新 {dates[-1]}" if dates and dates[-1] else ""
        return f"{len(self._domains)} 個網域{newest}"


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\W_]+", "", normalize_width(text)).lower()
    return {clean[index:index + 2] for index in range(len(clean) - 1)}


class RumorIndex:
    """165 闢謠專區的本機檢索（決策 D14）：字元雙連詞加 IDF，不需要額外套件，結果固定。標題命中的權重加倍。"""

    def __init__(self, rows: Iterable[tuple[str, str]] = ()) -> None:
        self.rows = list(rows)
        self._titles = [_bigrams(title) for title, _ in self.rows]
        self._grams = [titles | _bigrams(content) for titles, (_, content) in zip(self._titles, self.rows)]
        frequency = Counter(gram for grams in self._grams for gram in grams)
        self._idf = {gram: math.log(1 + len(self.rows) / count) for gram, count in frequency.items()}

    def __len__(self) -> int:
        return len(self.rows)

    def search(self, query: str, limit: int = 2) -> list[dict[str, str]]:
        wanted = _bigrams(query)
        scored = []
        for index, grams in enumerate(self._grams):
            shared = wanted & grams
            if len(shared) >= MIN_SHARED_GRAMS:
                score = sum(self._idf[gram] * (2 if gram in self._titles[index] else 1) for gram in shared)
                scored.append((-score, index))
        return [{"title": self.rows[index][0], "snippet": self.rows[index][1][:SNIPPET_CHARS]}
                for _, index in sorted(scored)[:limit]]

    @classmethod
    def from_cache(cls, cache_dir: Path, source: Source) -> RumorIndex:
        path = Path(cache_dir) / f"{source.id}.csv"
        if not path.is_file():
            return cls()
        with path.open(encoding="utf-8", newline="") as handle:
            return cls([(row.get("title") or "", row.get("content") or "") for row in csv.DictReader(handle)])

    def describe(self) -> str:
        return f"{len(self.rows)} 篇" if self.rows else "尚未下載（在自己電腦上執行 python -m guardian.data_165 --update）"


# ---------------------------------------------------------------- 命令列

STATUS_LABELS = {"updated": "已更新", "fresh": "不用更新", "failed": "失敗（沿用舊快取）"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m guardian.data_165", description="下載並快取 165 涉詐網站名單")
    parser.add_argument("--update", action="store_true", help="下載超過有效期限的名單")
    parser.add_argument("--force", action="store_true", help="不管有效期限，全部重新下載")
    parser.add_argument("--config", default=None, help="設定檔路徑")
    args = parser.parse_args(argv)

    from .config import ConfigError, load_config

    try:
        settings = load_config(args.config).data_165
    except ConfigError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    failed = False
    if args.update or args.force:
        for result in update(settings.cache_dir, [*settings.sources, settings.rumors],
                             max_age_days=settings.max_age_days, force=args.force):
            failed = failed or result.status == "failed"
            detail = f"：{result.message}" if result.message else ""
            unit = "篇" if result.source_id == settings.rumors.id else "個網域"
            print(f"{result.source_id}｜{STATUS_LABELS[result.status]}｜{result.domains} {unit}{detail}")
    print(f"165 名單：{DomainList.from_cache(settings.cache_dir, settings.sources).describe()}")
    print(f"165 闢謠專區：{RumorIndex.from_cache(settings.cache_dir, settings.rumors).describe()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
