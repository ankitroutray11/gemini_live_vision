"""Chirkoot Security Package.

Provides centralized security policy, Windows DPAPI vault, path validation,
taint tracking, sandboxed process execution, and emergency stop management.
"""

from security_policy import (
    ToolTier,
    TrustBoundary,
    UntrustedSourceType,
    PolicyDecision,
    PolicyEvaluationResult,
    CENTRAL_SECURITY_ENGINE,
    GLOBAL_TAINT_TRACKER,
    DPAPI_VAULT,
    validate_path_for_read,
    validate_path_for_write,
    is_sensitive_path,
    is_immutable_core_path,
    execute_sandboxed_command,
    execute_sandboxed_python_script,
    redact_secrets,
    record_audit_event,
    is_kill_switch_active,
    get_active_child_process_count,
    register_active_child_process,
    unregister_active_child_process,
    terminate_all_child_processes,
    CHIRKOOT_APPDATA_DIR,
    LOGS_DIR,
    MEMORY_DIR,
    CONFIG_DIR,
    TEMP_DIR,
    BACKUP_DIR,
    DATA_DIR,
    TIER_1_TOOLS,
    TIER_2_TOOLS,
    TIER_3_TOOLS
)

from emergency_stop import (
    EMERGENCY_STOP_MANAGER,
    EmergencyStopManager
)

__all__ = [
    "ToolTier",
    "TrustBoundary",
    "UntrustedSourceType",
    "PolicyDecision",
    "PolicyEvaluationResult",
    "CENTRAL_SECURITY_ENGINE",
    "GLOBAL_TAINT_TRACKER",
    "DPAPI_VAULT",
    "validate_path_for_read",
    "validate_path_for_write",
    "is_sensitive_path",
    "is_immutable_core_path",
    "execute_sandboxed_command",
    "execute_sandboxed_python_script",
    "redact_secrets",
    "record_audit_event",
    "is_kill_switch_active",
    "get_active_child_process_count",
    "register_active_child_process",
    "unregister_active_child_process",
    "terminate_all_child_processes",
    "CHIRKOOT_APPDATA_DIR",
    "LOGS_DIR",
    "MEMORY_DIR",
    "CONFIG_DIR",
    "TEMP_DIR",
    "BACKUP_DIR",
    "DATA_DIR",
    "TIER_1_TOOLS",
    "TIER_2_TOOLS",
    "TIER_3_TOOLS",
    "EMERGENCY_STOP_MANAGER",
    "EmergencyStopManager"
]
