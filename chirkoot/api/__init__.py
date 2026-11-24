"""Chirkoot API Package.

Provides HTTP REST endpoints, WebSocket protocol routing,
binary media packet framing, rate limiting, and origin/session authentication.
"""

from server import (
    app,
    validate_auth_token,
    validate_origin,
    get_launch_auth_token,
    register_client_session,
    unregister_client_session,
    GLOBAL_RATE_LIMITER
)

from frame_store import (
    MediaPacket,
    FrameType,
    MalformedPacketError
)

__all__ = [
    "app",
    "validate_auth_token",
    "validate_origin",
    "get_launch_auth_token",
    "register_client_session",
    "unregister_client_session",
    "GLOBAL_RATE_LIMITER",
    "MediaPacket",
    "FrameType",
    "MalformedPacketError"
]
