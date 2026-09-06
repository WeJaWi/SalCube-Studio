"""Create disposable before/after fixtures for Word preservation visual QA."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.delivery_doc import append_titles
from tests.test_delivery_preservation import fixture


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="dmiqwi-docx-qa-"))
    before = root / "before.docx"
    after = root / "after.docx"
    fixture(before)
    shutil.copy2(before, after)
    append_titles(["New Completed Video"], after)
    print(root)
