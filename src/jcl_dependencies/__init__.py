"""jcl_dependencies - recover what a JCL job or PROC actually does, and what it needs.

The COBOL says *what a program does*; it does not say *what dataset it does it to*. That
binding lives in JCL, and so does the rest of the operational truth a modernization needs:
which programs a job runs, in what order, under what conditions, which datasets flow from
one step to the next, and which control cards reshape the bytes on the way through.

This package answers those questions and retrieves what it needs to answer them. It is a
peer of ``cobol_xstate``, not a part of it: the two share only the estate-retrieval half
(``cobol_xstate_core``), so a JCL run carries no COBOL modeling engine, and neither
package imports the other. The one place they meet - binding a COBOL program's file
ddnames to real datasets - is :func:`jcl_dependencies.views.bind_cobol_artifacts`, which
takes a plain manifest **dict**, not a COBOL object. That is deliberate, and it is what
keeps this direction of the dependency from existing at all.

The crawl is an ASSEMBLY chain, not a scheduling one: a job pulls in cataloged PROCs,
INCLUDE members and control-card datasets, each of which can carry ``EXEC PGM=`` steps and
DD statements that appear nowhere in the JCL file itself. Parsed without them, those steps
do not show up as programs, as datasets, or at all - the job simply reads as far simpler
than it is. Nothing here follows job-to-job scheduling references (INTRDR, TWS, CA-7);
that is a different graph and this package does not pretend to model it.
"""

import logging as _logging

#: This package's top-level logger name. The CLI passes it - alongside core's own root -
#: to ``cobol_xstate_core.logging_setup.configure_logging``; a root nobody configures
#: either propagates to the root logger or prints via logging's lastResort.
PACKAGE_LOGGER = "jcl_dependencies"

_logging.getLogger(PACKAGE_LOGGER).addHandler(_logging.NullHandler())

from .parser import DD, DDSegment, Job, ProcDef, Step, parse_jcl   # noqa: E402
from .prefetch import prefetch_jcl                                 # noqa: E402
from .views import (bind_cobol_artifacts, build_jcl_artifacts,     # noqa: E402
                    build_jcl_lineage)

#: The version of the COBOL-artifact-manifest contract :func:`bind_cobol_artifacts` binds
#: against. The orchestrator that owns ``--bind-jcl`` checks it at import time, so a
#: skewed pair of packages fails loudly instead of quietly producing an unbound manifest -
#: which would look FINE, because an unbound manifest says exactly what an unbound run says.
BIND_API_VERSION = 1

__all__ = [
    "parse_jcl", "Job", "Step", "DD", "DDSegment", "ProcDef",
    "build_jcl_lineage", "build_jcl_artifacts", "bind_cobol_artifacts",
    "prefetch_jcl", "PACKAGE_LOGGER", "BIND_API_VERSION",
]

__version__ = "0.1.0"
