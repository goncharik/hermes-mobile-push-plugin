"""Dashboard backend entry for hermes-push.

The Hermes web server imports the manifest's ``api`` file by path and reads its
module-level ``router`` attribute (hermes_cli/web_server.py::
_mount_plugin_api_routes). That importer validates the ``api`` path stays
*inside* the plugin's ``dashboard/`` directory and rejects ``..`` traversal, so
the file the manifest points at must live here — it cannot point directly at
``hermes_push/api.py`` one level up.

This shim therefore re-exports the real router from the installed
``hermes_push`` package, keeping the route implementation in the package
(testable via ``import hermes_push.api``) while satisfying the host's
dashboard-dir constraint.
"""

from __future__ import annotations

from hermes_push.api import router  # noqa: F401  (re-exported for the host)
