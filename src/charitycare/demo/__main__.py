"""``python -m charitycare.demo [OUT_DIR]``: build the synthetic corpus."""

import sys
from pathlib import Path

from charitycare.demo.build import build_corpus

if __name__ == "__main__":
    m = build_corpus(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(f"built {len(m['hospitals'])} hospitals, {m['files']} files, checksum {m['checksum'][:16]}")
