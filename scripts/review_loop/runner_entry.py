"""Run ``runner_cli`` as a file: ``python3 -I scripts/review_loop/runner_entry.py ...``.

How the full-mode runner calls this package. ``-I`` keeps PYTHONPATH, the user's site
directory and the working directory off the path, and this puts only the ``scripts``
package there: the checkout's root never is, so a ``json.py`` at its top cannot stand
in for the standard library.
"""

import importlib
import sys
import types
from pathlib import Path

if __name__ == "__main__":
    package = types.ModuleType("scripts")
    package.__path__ = [str(Path(__file__).resolve().parent.parent)]
    sys.modules["scripts"] = package
    cli = importlib.import_module("scripts.review_loop.runner_cli")
    sys.exit(cli.main(sys.argv[1:]))
