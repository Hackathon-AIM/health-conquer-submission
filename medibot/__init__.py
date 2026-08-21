"""Development shim so `python -m medibot...` works from a source checkout."""

from pathlib import Path

_src_pkg = Path(__file__).resolve().parents[1] / "src" / "medibot"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))

__version__ = "0.1.0"
