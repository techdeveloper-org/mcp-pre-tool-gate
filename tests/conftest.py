"""
conftest.py - Shared pytest fixtures for mcp-pre-tool-gate.

Adds the repository root to sys.path so test modules can `import server`
directly, matching the way server.py resolves its own sibling imports
(base/, utils/) via a root-relative sys.path insert at import time.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
