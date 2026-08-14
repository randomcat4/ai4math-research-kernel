"""Internal external-tool adapters.

These names are not part of the public ``rk`` package interface.  The host resolves them from
registered profiles and passes them to the kernel through the ``ExecutionAdapter`` seam.
"""

from rk.adapters.archon import ArchonAdapter
from rk.adapters.attestation import (
    IndependentVerifierArtifactAdapter,
    VerifierIdentity,
)
from rk.adapters.base import (
    AdapterConfigurationError,
    AdapterProfile,
    AdapterRequestError,
    CurlHttpClient,
    HttpClient,
    HttpResponse,
    ProcessResult,
    ProcessRunner,
    SafeSubprocessRunner,
    UrlLibHttpClient,
)
from rk.adapters.deepseek_responses import DeepSeekResponsesControllerAdapter
from rk.adapters.deterministic import RegisteredFileToolAdapter
from rk.adapters.jixia import JixiaAdapter
from rk.adapters.lean import LeanReplayAdapter
from rk.adapters.leansearch import LeanSearchAdapter
from rk.adapters.literature import CrossrefLiteratureAdapter
from rk.adapters.local_proof_model import LocalProofModelAdapter
from rk.adapters.openai_compatible import OpenAICompatibleAdapter
from rk.adapters.opencode import OpenCodeAdapter
from rk.adapters.rethlas import RethlasAdapter

__all__ = [
    "AdapterConfigurationError",
    "AdapterProfile",
    "AdapterRequestError",
    "ArchonAdapter",
    "CrossrefLiteratureAdapter",
    "CurlHttpClient",
    "DeepSeekResponsesControllerAdapter",
    "HttpClient",
    "HttpResponse",
    "IndependentVerifierArtifactAdapter",
    "JixiaAdapter",
    "LeanReplayAdapter",
    "LeanSearchAdapter",
    "LocalProofModelAdapter",
    "OpenAICompatibleAdapter",
    "OpenCodeAdapter",
    "ProcessResult",
    "ProcessRunner",
    "RegisteredFileToolAdapter",
    "RethlasAdapter",
    "SafeSubprocessRunner",
    "UrlLibHttpClient",
    "VerifierIdentity",
]
