"""Single source of truth for the agent version string.

Read by :mod:`proxialpha_agent.__init__`, the ``proxialpha version``
CLI subcommand, the heartbeat request payload, the ``/health``
endpoint, and the Docker image label.

We keep this in its own module (rather than embedded in
``__init__.py``) so importing the version string is free — no
transitive import of supervisor, licence client, or httpx. The
``doctor`` bundle command and cheap CLI paths rely on that.

Release cadence
---------------

* ``X.Y.Z-rc.N`` — release candidate; published to the private
  registry only, never tagged ``latest``.
* ``X.Y.Z`` — stable; eligible for ``latest`` via manual approval
  on the GitHub Actions ``workflow_dispatch`` path.

Do not hand-edit during a CI run. The release workflow has a
dedicated step that bumps this file via ``git`` + ``gh pr`` and
opens a PR — CI should never modify its own tree in-place.
"""
from __future__ import annotations

#: The current agent version. Keep in sync with the ``agent-v*``
#: git tag used by the CI workflow.
__version__ = "1.0.0-rc.1"

__all__ = ["__version__"]
