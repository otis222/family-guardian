"""寫檔的共用工具。"""

from __future__ import annotations

import time
from pathlib import Path

REPLACE_ATTEMPTS = 5
REPLACE_WAIT_SECONDS = 0.1


def write_text_atomic(path: str | Path, text: str) -> None:
    """先寫暫存檔再換名，關機時不會留下寫一半的檔案。

    Windows 上防毒軟體或索引服務開著檔案時，換名會暫時失敗，所以多試幾次；這是本機檔案，次數有上限。
    """
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT_SECONDS)
