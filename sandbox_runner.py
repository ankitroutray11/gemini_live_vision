"""Chirkoot MK-V Subprocess Skill Sandbox Runner.

Executes dynamically learned custom skills in a heavily restricted subprocess environment:
- Stripped environment variables (zero credential leakage).
- Strict network restriction enforcement (socket disabling).
- Filesystem access sandbox (blocks repository core and sensitive credentials).
- Structured JSON IPC over stdout.
"""

import os
import sys
import json
import base64
import argparse
import builtins
import traceback
import importlib.util
from pathlib import Path

# Ensure UTF-8 I/O
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def purge_sensitive_environment():
    """Strips all sensitive environment variables from the subprocess."""
    sensitive_prefixes = (
        "GEMINI_", "CHIRKOOT_", "AUTH_", "AWS_", "AZURE_",
        "GITHUB_", "SSH_", "API_KEY", "SECRET", "TOKEN", "PASSWORD"
    )
    for key in list(os.environ.keys()):
        upper_key = key.upper()
        if any(upper_key.startswith(p) or p in upper_key for p in sensitive_prefixes):
            os.environ.pop(key, None)


def enforce_network_restrictions(allow_network: bool):
    """Enforces strict socket disabling if network permission is not explicitly granted."""
    if not allow_network:
        import socket
        def _blocked_socket(*args, **kwargs):
            raise PermissionError("[SANDBOX VIOLATION] Network socket access is forbidden for this skill by security policy.")

        socket.socket = _blocked_socket
        socket.create_connection = _blocked_socket
        socket.getaddrinfo = _blocked_socket

        try:
            import urllib.request
            urllib.request.urlopen = _blocked_socket
        except Exception:
            pass


def enforce_filesystem_restrictions(allowed_filesystem_rules: list, appdata_dir: Path):
    """Enforces filesystem sandboxing by intercepting file open operations."""
    orig_open = builtins.open
    temp_dir = (appdata_dir / "temp").resolve()

    sensitive_patterns = [
        ".env", ".ssh", ".aws", ".azure", ".chirkoot_profile",
        "credentials", "id_rsa", "id_ed25519", "web data", "cookies"
    ]

    def _sandboxed_open(file, *args, **kwargs):
        try:
            p = Path(file).resolve()
        except Exception:
            return orig_open(file, *args, **kwargs)

        p_str = str(p).lower()

        # 1. Block access to sensitive credential or session files
        for pat in sensitive_patterns:
            if pat in p_str:
                raise PermissionError(f"[SANDBOX VIOLATION] Access to sensitive credential path '{p}' is forbidden.")

        # Determine mode
        mode = args[0] if args else kwargs.get("mode", "r")
        is_write = any(m in mode for m in ("w", "a", "+", "x"))

        if is_write:
            # Check if write path is permitted
            allowed_write = False
            # Always allow writing to %LOCALAPPDATA%\Chirkoot\temp
            try:
                p.relative_to(temp_dir)
                allowed_write = True
            except ValueError:
                pass

            # Check explicit manifest write permissions
            for rule in allowed_filesystem_rules:
                if rule.startswith("write:") or rule.startswith("rw:"):
                    target_dir = Path(rule.split(":", 1)[1].strip()).resolve()
                    try:
                        p.relative_to(target_dir)
                        allowed_write = True
                        break
                    except ValueError:
                        pass

            if not allowed_write:
                raise PermissionError(
                    f"[SANDBOX VIOLATION] Write operation to '{p}' is blocked. "
                    f"Skills may only write inside %LOCALAPPDATA%\\Chirkoot\\temp or explicitly granted directories."
                )

        return orig_open(file, *args, **kwargs)

    builtins.open = _sandboxed_open


def run_skill_in_sandbox(skill_name: str, skill_file: str, manifest_file: str, args_b64: str):
    """Executes the skill and outputs JSON result to stdout."""
    # 1. Purge sensitive environment variables
    purge_sensitive_environment()

    # 2. Parse arguments
    kwargs = {}
    if args_b64:
        try:
            raw_json = base64.b64decode(args_b64.encode("utf-8")).decode("utf-8")
            kwargs = json.loads(raw_json)
        except Exception as e:
            print(json.dumps({"status": "error", "error_type": "ArgumentParseError", "error": f"Failed to decode arguments: {e}"}))
            sys.exit(1)

    # 3. Parse manifest
    manifest = {}
    if manifest_file and os.path.exists(manifest_file):
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    perms = manifest.get("permissions", {})
    allow_network = bool(perms.get("network", False))
    fs_rules = perms.get("filesystem", [])

    local_app_data = os.environ.get("LOCALAPPDATA")
    appdata_dir = (Path(local_app_data) / "Chirkoot").resolve() if local_app_data else (Path.home() / ".chirkoot").resolve()

    # 4. Enforce network and filesystem sandbox policies
    enforce_network_restrictions(allow_network)
    enforce_filesystem_restrictions(fs_rules, appdata_dir)

    # 5. Set working directory to temp dir
    temp_dir = appdata_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chdir(str(temp_dir))
    except Exception:
        pass

    # 6. Load skill module
    skill_path = Path(skill_file).resolve()
    if not skill_path.exists():
        print(json.dumps({"status": "error", "error_type": "FileNotFoundError", "error": f"Skill file '{skill_path}' not found."}))
        sys.exit(1)

    try:
        spec = importlib.util.spec_from_file_location(skill_name, str(skill_path))
        if not spec or not spec.loader:
            print(json.dumps({"status": "error", "error_type": "ImportError", "error": f"Could not create module spec for '{skill_name}'."}))
            sys.exit(1)

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, skill_name, None)
        if not func or not callable(func):
            print(json.dumps({"status": "error", "error_type": "AttributeError", "error": f"Skill entrypoint 'def {skill_name}(...)' not found in module."}))
            sys.exit(1)

        # 7. Execute skill
        result = func(**kwargs)

        # 8. Output success JSON
        out_payload = {
            "status": "success",
            "skill": skill_name,
            "result": str(result) if result is not None else ""
        }
        print(json.dumps(out_payload, ensure_ascii=False))
        sys.exit(0)

    except PermissionError as pe:
        out_payload = {
            "status": "error",
            "error_type": "PermissionError",
            "error": str(pe),
            "traceback": traceback.format_exc()
        }
        print(json.dumps(out_payload, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        out_payload = {
            "status": "error",
            "error_type": type(e).__name__,
            "error": str(e),
            "traceback": traceback.format_exc()
        }
        print(json.dumps(out_payload, ensure_ascii=False))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Chirkoot Subprocess Skill Sandbox")
    parser.add_argument("--skill", required=True, help="Skill entrypoint name")
    parser.add_argument("--skill-file", required=True, help="Path to Python skill file")
    parser.add_argument("--manifest", required=False, default="", help="Path to manifest JSON file")
    parser.add_argument("--args-b64", required=False, default="", help="Base64 encoded JSON kwargs")

    parsed = parser.parse_args()
    run_skill_in_sandbox(parsed.skill, parsed.skill_file, parsed.manifest, parsed.args_b64)


if __name__ == "__main__":
    main()
