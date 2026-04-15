"""Broker-side Linux hardening: deny ptrace-style memory reads + core dumps.

Called once at broker startup, after ingest_secrets_fd and before the
worker is spawned. Silent on non-Linux; production deploys are the
Docker/Fly target where these calls matter.
"""

from __future__ import annotations

import resource
import sys


def harden_broker() -> None:
    """Close artifact-level leaks:

    - `PR_SET_DUMPABLE(0)` flips the dumpable flag off. Same-UID callers
      can no longer open `/proc/<broker>/mem` or `process_vm_readv()`
      even under relaxed `ptrace_scope`. It also sanitizes ownership of
      `/proc/<pid>/*` entries to root:root so a same-UID worker can't
      read many of them.
    - `RLIMIT_CORE=0` ensures that if broker ever crashes, the kernel
      does NOT write a core file containing heap memory (which holds
      api_key as a Python string).

    Silent on non-Linux. Logs a stderr warning if the prctl call
    returns non-zero (operator can detect misconfigured seccomp/libc).
    """
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass

    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_DUMPABLE = 4
        rc = libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        if rc != 0:
            errno = ctypes.get_errno()
            print(
                f"[broker] warning: PR_SET_DUMPABLE failed rc={rc} errno={errno}. "
                "Same-UID /proc memory reads may not be blocked.",
                file=sys.stderr,
                flush=True,
            )
    except (OSError, AttributeError):
        pass
