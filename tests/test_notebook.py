"""notebook（SPEC 第 13 節第 13 項）：沒有執行輸出、沒有金鑰；第一格是安裝指令；.py 和 .ipynb 同步。"""

import json
import re
from pathlib import Path

import jupytext
import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "notebooks" / "guardian_demo.py"
NOTEBOOK = ROOT / "notebooks" / "guardian_demo.ipynb"
SECRET_PATTERNS = (
    r"gsk_[A-Za-z0-9]{10,}",  # Groq
    r"sk-[A-Za-z0-9_-]{10,}",  # OpenAI、Anthropic
    r"\b[UC][0-9a-f]{32}\b",  # LINE 的使用者和群組編號
    r"\b[0-9a-f]{32}\b",  # channel secret
    r"[A-Za-z0-9+/]{100,}",  # channel access token
    r"(?:API_KEY|SECRET|ACCESS_TOKEN|HASH_SALT)['\"]?\s*[=:]\s*['\"]\S",  # 把金鑰寫死
)


def cell_text(cell):
    source = cell["source"]
    return source if isinstance(source, str) else "".join(source)


@pytest.fixture(scope="module")
def notebook():
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def code_cells(notebook):
    return [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]


def test_no_outputs_are_left_behind(code_cells):
    assert code_cells
    for cell in code_cells:
        assert cell["outputs"] == [] and cell["execution_count"] is None


@pytest.mark.parametrize("path", [SOURCE, NOTEBOOK])
def test_no_keys_or_line_ids(path):
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not re.search(pattern, text), pattern


def test_first_cell_installs_the_course_and_project_packages(notebook):
    first = notebook["cells"][0]
    assert first["cell_type"] == "code"
    text = cell_text(first)
    assert "!pip install" in text
    section = ROOT.joinpath("requirements.txt").read_text(encoding="utf-8").split("# 開發與測試")[0]
    requirements = [line for line in section.splitlines() if line and not line.startswith("#")]
    assert len(requirements) >= 6
    for requirement in requirements:
        pinned = "==" in requirement
        assert (requirement if pinned else re.split(r"[<>=]", requirement)[0]) in text, requirement


def test_ipynb_matches_the_source(notebook):
    expected = jupytext.read(SOURCE)
    assert [(cell.cell_type, cell.source) for cell in expected.cells] == [
        (cell["cell_type"], cell_text(cell)) for cell in notebook["cells"]]


def test_uses_replay_and_needs_no_key(code_cells):
    text = "\n".join(cell_text(cell) for cell in code_cells)
    assert 'llm_mode="replay"' in text
    for word in ("load_env_file", "userdata", "getpass", "os.environ"):
        assert word not in text


def test_clone_url_has_no_credentials(code_cells):
    urls = re.findall(r"https?://[^\s\"']+", "\n".join(cell_text(cell) for cell in code_cells))
    assert urls and all("@" not in url for url in urls)


def test_code_cells_are_valid_python(code_cells):
    for cell in code_cells:
        lines = [line for line in cell_text(cell).splitlines() if not line.startswith(("!", "%"))]
        compile("\n".join(lines), "notebook-cell", "exec")
