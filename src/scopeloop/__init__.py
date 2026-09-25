"""ScopeLoop - Hardware development automation platform for LLM coding agents."""

__version__ = "0.2.0"

from scopeloop.config import Config, load_config
from scopeloop.session import Session, SessionManager

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "Session",
    "SessionManager",
]
