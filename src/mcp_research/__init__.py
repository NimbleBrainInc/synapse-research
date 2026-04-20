"""synapse-research — MCP tasks dial-tone app.

Exposes a single task-augmented tool (`start_research`) that simulates a
long-running research workflow. The real research implementation swaps in
at `mcp_research.worker` once dial tone is validated.
"""

__version__ = "0.1.1"
