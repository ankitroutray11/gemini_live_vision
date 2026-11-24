"""Chirkoot Automation Package.

Provides native Windows OS control, UI Automation (UIA), Set-of-Mark visual grounding,
closed-loop action verification, and clipboard state preservation.
"""

from desktop_automation import (
    DpiCoordinateManager,
    RobustWindowSwitcher,
    UnicodeInputEngine,
    UiaEngine,
    SetOfMarkEngine,
    GLOBAL_SOM_ENGINE,
    GLOBAL_AUTOMATION_ENGINE,
    AutomationPriorityTier
)

from closed_loop_verifier import (
    GLOBAL_CLOSED_LOOP_VERIFIER,
    GLOBAL_CLIPBOARD_PRESERVER,
    ActionIntention,
    VerificationOutcome,
    VerificationEvidenceType,
    VerificationResult,
    ClosedLoopActionVerifier
)

__all__ = [
    "DpiCoordinateManager",
    "RobustWindowSwitcher",
    "UnicodeInputEngine",
    "UiaEngine",
    "SetOfMarkEngine",
    "GLOBAL_SOM_ENGINE",
    "GLOBAL_AUTOMATION_ENGINE",
    "AutomationPriorityTier",
    "GLOBAL_CLOSED_LOOP_VERIFIER",
    "GLOBAL_CLIPBOARD_PRESERVER",
    "ActionIntention",
    "VerificationOutcome",
    "VerificationEvidenceType",
    "VerificationResult",
    "ClosedLoopActionVerifier"
]
