"""Test bootstrap for the flat (directory-plugin) layout.

The repo root IS the ``hermes_push`` package: its ``__init__.py`` lives at the
repo root and its modules use RELATIVE imports (``from .store import ...``) so
the agent's directory loader can import it as ``hermes_plugins.hermes_push``
with ``submodule_search_locations=[<repo>]``.

setuptools' editable-install finder imports such a root-mapped package WITHOUT
``submodule_search_locations``, so it is not treated as a package and the
relative imports fail. Rather than depend on that quirk, we register
``hermes_push`` here the SAME way the agent does — ``spec_from_file_location``
with ``submodule_search_locations`` — so ``import hermes_push`` /
``from hermes_push.store import ...`` resolve correctly and the suite exercises
the real (directory) load path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG = "hermes_push"

if _PKG not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _PKG,
        _REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = module
    spec.loader.exec_module(module)
