"""Internal external-tool adapters.

These names are not part of the public ``rk`` package interface.  The host resolves them from
registered profiles and passes them to the kernel through the ``ExecutionAdapter`` seam.
"""

from rk.adapters.archon import ArchonAdapter
from rk.adapters.base import (
    AdapterConfigurationError,
    AdapterProfile,
    AdapterRequestError,
    HttpClient,
    HttpResponse,
    ProcessResult,
    ProcessRunner,
    SafeSubprocessRunner,
    UrlLibHttpClient,
)
from rk.adapters.jixia import JixiaAdapter
from rk.adapters.leansearch import LeanSearchAdapter
from rk.adapters.rethlas import RethlasAdapter

__all__ = [
    "AdapterConfigurationError",
    "AdapterProfile",
    "AdapterRequestError",
    "ArchonAdapter",
    "HttpClient",
    "HttpResponse",
    "JixiaAdapter",
    "LeanSearchAdapter",
    "ProcessResult",
    "ProcessRunner",
    "RethlasAdapter",
    "SafeSubprocessRunner",
    "UrlLibHttpClient",
]
