def get_system_uptime() -> str:
    """Calculates how long the Windows host system has been running."""
    import psutil
    from datetime import datetime
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    delta = datetime.now() - boot_time
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    mins, secs = divmod(rem, 60)
    return f"System uptime: {hours}h {mins}m {secs}s (Booted at {boot_time.strftime('%Y-%m-%d %H:%M:%S')})"
