"""命令列 python -m guardian.run。"""

import os
import subprocess
import sys

import pytest

from guardian import run
from guardian.config import PROJECT_ROOT

DEMO = str(PROJECT_ROOT / "scenarios" / "demo_family.yaml")


@pytest.fixture(autouse=True)
def skip_dotenv(monkeypatch):
    # 測試不讀開發者自己的 .env
    monkeypatch.setattr(run, "load_env_file", lambda *args, **kwargs: False)


def test_cli_offline_run(capsys):
    assert run.main([DEMO, "--mode", "offline"]) == 0
    out = capsys.readouterr().out
    assert "事件：21 則" in out
    assert "快速過濾：不處理 14｜觀察 1｜候選 5" in out
    assert "候選佇列：進過 5 則，還留 0 則" in out
    assert "守護人報告：6 則　群組回覆：1 則" in out
    assert "模型呼叫：" in out and "模型呼叫：0 次" not in out
    assert "165 名單：" in out and "165 闢謠專區：" in out
    assert "Watchdog：" in out and "模型正常" in out and "本月推播 6／200 則" in out  # 6 則都是 high
    assert "回覆 m09（model＋圖卡）" in out
    assert "今日摘要" not in out


def test_cli_daily_summary_has_no_message_text(capsys):
    assert run.main([DEMO, "--mode", "offline", "--daily-summary"]) == 0
    out = capsys.readouterr().out
    assert "顧家貓今日摘要" in out and "高風險：" in out
    assert "保證獲利穩賺" not in out and "http" not in out


@pytest.mark.parametrize("encoding", ["cp950", "cp936"])
def test_cli_output_redirected_on_chinese_windows_does_not_crash(encoding):
    env = {**os.environ, "PYTHONIOENCODING": encoding}
    result = subprocess.run([sys.executable, "-m", "guardian.run", DEMO, "--mode", "offline", "--trace",
                             "--daily-summary"], cwd=PROJECT_ROOT, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(encoding, "replace")


def test_cli_trace_lists_every_event_without_text(capsys):
    assert run.main([DEMO, "--mode", "offline", "--trace"]) == 0
    out = capsys.readouterr().out
    for index in range(1, 22):
        assert f"m{index:02d}" in out
    assert "保證獲利" not in out and "請不要提醒家人" not in out  # 不印訊息原文


def test_cli_speaking_mode_override(capsys):
    assert run.main([DEMO, "--mode", "offline", "--speaking", "gentle"]) == 0
    out = capsys.readouterr().out
    assert "發言模式：gentle" in out and "群組回覆：6 則" in out


def test_cli_reports_errors_without_traceback(capsys, tmp_path):
    assert run.main([str(tmp_path / "nope.yaml"), "--mode", "offline"]) == 1
    assert "錯誤：找不到劇本" in capsys.readouterr().err
