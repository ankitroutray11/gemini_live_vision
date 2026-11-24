"""
Chirkoot MK-V Windows Deployment, Packaging & Lifecycle Subsystem.
===================================================================
Provides hardened deployment operations:
1. Separation of application installation from mutable user data (%LOCALAPPDATA%\\Chirkoot).
2. Cryptographically signed update manifests and SHA-256 package verification.
3. Staged update application with automatic rollback to Last-Known-Good (LKG) on failed health check.
4. Win32 Single-Instance Mutex enforcement and existing window foreground activation.
5. Watchdog crash-loop recovery and safe-mode escalation.
6. Controlled process lifecycle and graceful shutdown.
"""

import base64
import ctypes
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger("ChirkootDeployment")

# -----------------------------------------------------------------------------
# 1. DIRECTORY ARCHITECTURE & IMMUTABILITY BOUNDARIES
# -----------------------------------------------------------------------------

def get_installation_dir() -> Path:
    """Returns the protected, read-only application installation directory."""
    # If running from a frozen bundle (PyInstaller)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def get_user_data_dir() -> Path:
    """Returns the agent and user writable data root in %LOCALAPPDATA%\\Chirkoot."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data) / "Chirkoot"
    else:
        base = Path.home() / "AppData" / "Local" / "Chirkoot"
    return base

INSTALL_DIR = get_installation_dir()
USER_DATA_DIR = get_user_data_dir()

# Structured subdirectories inside %LOCALAPPDATA%\Chirkoot
DATA_DIR = USER_DATA_DIR / "data"
LOGS_DIR = USER_DATA_DIR / "logs"
CONFIG_DIR = USER_DATA_DIR / "config"
BACKUPS_DIR = USER_DATA_DIR / "backups"
STAGING_DIR = USER_DATA_DIR / "staging"
BROWSER_PROFILE_DIR = USER_DATA_DIR / "browser_profile"
TEMP_DIR = USER_DATA_DIR / "temp"
VAULT_DIR = USER_DATA_DIR / "vault"
SKILLS_DIR = USER_DATA_DIR / "custom_skills"

def ensure_user_data_directories():
    """Initializes all mutable user data directories in %LOCALAPPDATA%\\Chirkoot."""
    for d in [
        DATA_DIR,
        LOGS_DIR,
        CONFIG_DIR,
        BACKUPS_DIR,
        STAGING_DIR,
        BROWSER_PROFILE_DIR,
        TEMP_DIR,
        VAULT_DIR,
        SKILLS_DIR
    ]:
        d.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# 2. SHA-256 INTEGRITY & CRYPTOGRAPHIC SIGNATURE ENGINE
# -----------------------------------------------------------------------------

def calculate_file_sha256(file_path: Path) -> str:
    """Calculates the SHA-256 hexadecimal hash of a file using streaming reads."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def generate_keypair() -> Tuple[bytes, bytes]:
    """Generates an RSA 2048-bit keypair for signing update manifests."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem


def sign_manifest(manifest_data: Dict[str, Any], private_key_pem: bytes) -> str:
    """Signs canonical JSON manifest data using RSA-PSS SHA-256."""
    canonical_bytes = json.dumps(
        {k: v for k, v in manifest_data.items() if k != "signature"},
        sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")

    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = private_key.sign(
        canonical_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode("ascii")


def verify_manifest_signature(manifest_data: Dict[str, Any], public_key_pem: bytes) -> bool:
    """Verifies RSA-PSS digital signature of update manifest."""
    sig_b64 = manifest_data.get("signature")
    if not sig_b64:
        logger.warning("[Update Verification] Manifest is missing a digital signature.")
        return False

    try:
        signature = base64.b64decode(sig_b64)
        canonical_bytes = json.dumps(
            {k: v for k, v in manifest_data.items() if k != "signature"},
            sort_keys=True,
            separators=(",", ":")
        ).encode("utf-8")

        public_key = serialization.load_pem_public_key(public_key_pem)
        public_key.verify(
            signature,
            canonical_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return True
    except (InvalidSignature, Exception) as e:
        logger.warning(f"[Update Verification] Invalid manifest signature: {e}")
        return False


def create_update_manifest(
    source_dir: Path,
    version: str,
    private_key_pem: bytes,
    file_relpaths: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Builds a signed update manifest for all specified files in source_dir."""
    files_dict = {}
    if file_relpaths is None:
        file_relpaths = []
        for p in source_dir.rglob("*"):
            if p.is_file() and not p.name.endswith(".pyc") and ".git" not in p.parts:
                file_relpaths.append(str(p.relative_to(source_dir)).replace("\\", "/"))

    for rel in sorted(file_relpaths):
        full_p = source_dir / rel
        if full_p.is_file():
            files_dict[rel] = {
                "sha256": calculate_file_sha256(full_p),
                "size": full_p.stat().st_size
            }

    manifest = {
        "manifest_version": "1.0",
        "package_name": "chirkoot-mkv",
        "version": version,
        "timestamp": time.time(),
        "files": files_dict
    }
    manifest["signature"] = sign_manifest(manifest, private_key_pem)
    return manifest


def build_update_package(
    source_dir: Path,
    output_zip_path: Path,
    manifest: Dict[str, Any]
) -> Path:
    """Packages files and signed update_manifest.json into an update zip archive."""
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write manifest first
        zf.writestr("update_manifest.json", json.dumps(manifest, indent=2))
        for rel_path in manifest["files"].keys():
            full_path = source_dir / rel_path
            if full_path.exists():
                zf.write(full_path, rel_path)
    return output_zip_path


def verify_and_stage_update(
    update_zip_path: Path,
    staging_target_dir: Path,
    trusted_public_key_pem: bytes
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Safely unpacks, cryptographically validates, and checks SHA-256 integrity of an update.
    Rejects unsigned, untrusted, or corrupted update packages.
    """
    if not update_zip_path.exists():
        return False, f"Update package '{update_zip_path}' not found.", None

    if staging_target_dir.exists():
        shutil.rmtree(staging_target_dir, ignore_errors=True)
    staging_target_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Extract archive to staging area
        with zipfile.ZipFile(update_zip_path, "r") as zf:
            # Check for path traversal attempts in zip archive entries
            for entry in zf.namelist():
                if ".." in entry or entry.startswith("/") or entry.startswith("\\"):
                    shutil.rmtree(staging_target_dir, ignore_errors=True)
                    return False, f"Malicious path traversal detected in zip archive: '{entry}'", None
            zf.extractall(staging_target_dir)

        # 2. Read and verify manifest
        manifest_path = staging_target_dir / "update_manifest.json"
        if not manifest_path.exists():
            shutil.rmtree(staging_target_dir, ignore_errors=True)
            return False, "Package is missing update_manifest.json.", None

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        if not verify_manifest_signature(manifest, trusted_public_key_pem):
            shutil.rmtree(staging_target_dir, ignore_errors=True)
            return False, "[SECURITY REJECTION] Package signature verification failed. Untrusted update rejected.", None

        # 3. Verify SHA-256 integrity of each extracted file
        for rel_path, file_meta in manifest.get("files", {}).items():
            extracted_file = staging_target_dir / rel_path
            if not extracted_file.exists():
                shutil.rmtree(staging_target_dir, ignore_errors=True)
                return False, f"Package file missing from extracted payload: '{rel_path}'", None

            actual_sha = calculate_file_sha256(extracted_file)
            expected_sha = file_meta.get("sha256", "")
            if actual_sha.lower() != expected_sha.lower():
                shutil.rmtree(staging_target_dir, ignore_errors=True)
                return False, f"[INTEGRITY ERROR] SHA-256 mismatch for '{rel_path}': expected {expected_sha[:12]}, got {actual_sha[:12]}", None

        logger.info(f"⚡ [Update Staged] Successfully staged and verified update v{manifest.get('version')} ({len(manifest.get('files', {}))} files)")
        return True, "Update package successfully verified and staged.", manifest

    except Exception as e:
        shutil.rmtree(staging_target_dir, ignore_errors=True)
        return False, f"Error processing update package: {e}", None


def seed_initial_lkg_backup(install_dir: Path, lkg_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Seeds the initial Last-Known-Good backup of core files in %LOCALAPPDATA%\\Chirkoot\\backups\\lkg_core."""
    if lkg_dir is None:
        lkg_dir = BACKUPS_DIR / "lkg_core"
    return create_lkg_backup(install_dir, lkg_dir)


def generate_installation_manifests(
    install_dir: Path,
    version: str = "5.0.0",
    core_only: bool = False
) -> Tuple[Path, Path]:
    """
    Generates manifest.sha256 and installed_manifest.json in install_dir.
    Lists critical Python and executable files with their SHA-256 hashes.
    """
    sha_lines = []
    json_files = {}

    target_files = []
    if core_only:
        core_names = [
            "desktop_app.py", "server.py", "security_policy.py",
            "learning_engine.py", "ChirkootLauncher.cs", "ChirkootLauncher.exe"
        ]
        for name in core_names:
            p = install_dir / name
            if p.exists():
                target_files.append(p)
    else:
        for p in install_dir.rglob("*"):
            if p.is_file() and not p.name.endswith(".pyc") and ".git" not in p.parts:
                target_files.append(p)

    for p in sorted(target_files):
        rel = str(p.relative_to(install_dir)).replace("\\", "/")
        sha = calculate_file_sha256(p)
        size = p.stat().st_size
        sha_lines.append(f"{sha}  {rel}")
        json_files[rel] = {
            "sha256": sha,
            "size": size
        }

    sha256_path = install_dir / "manifest.sha256"
    with open(sha256_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sha_lines) + "\n")

    manifest_json = {
        "manifest_version": "1.0",
        "package_name": "chirkoot-mkv",
        "version": version,
        "generated_at": time.time(),
        "files": json_files
    }
    json_path = install_dir / "installed_manifest.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest_json, f, indent=2)

    logger.info(f"📜 [Manifest Generated] Wrote {len(json_files)} hashes to {sha256_path.name} and {json_path.name}")
    return sha256_path, json_path


# -----------------------------------------------------------------------------
# 3. LKG BACKUP, APPLICATION & ROLLBACK ENGINE
# -----------------------------------------------------------------------------

def create_lkg_backup(install_dir: Path, backup_dest_dir: Path) -> Tuple[bool, str]:
    """Snapshots current verified application files to %LOCALAPPDATA%\\Chirkoot\\backups\\lkg_core."""
    try:
        if backup_dest_dir.exists():
            shutil.rmtree(backup_dest_dir, ignore_errors=True)
        backup_dest_dir.mkdir(parents=True, exist_ok=True)

        copied = 0
        for p in install_dir.rglob("*"):
            if p.is_file() and not p.name.endswith(".pyc") and ".git" not in p.parts:
                rel = p.relative_to(install_dir)
                dest = backup_dest_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                copied += 1

        logger.info(f"📸 [LKG Snapshot] Created Last-Known-Good backup ({copied} files) at: {backup_dest_dir}")
        return True, f"Created LKG backup of {copied} files."
    except Exception as e:
        return False, f"Error creating LKG backup: {e}"


def apply_staged_update(staging_dir: Path, install_dir: Path) -> Tuple[bool, str]:
    """Copies verified staged files into the application installation directory."""
    try:
        manifest_path = staging_dir / "update_manifest.json"
        if manifest_path.exists():
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            files_to_copy = list(manifest.get("files", {}).keys())
            version_str = manifest.get("version", "unknown")
            shutil.copy2(manifest_path, install_dir / "installed_manifest.json")
        else:
            files_to_copy = [
                str(p.relative_to(staging_dir)).replace("\\", "/")
                for p in staging_dir.rglob("*")
                if p.is_file() and p.name != "update_manifest.json"
            ]
            version_str = "staged_payload"

        applied = 0
        for rel_path in files_to_copy:
            staged_file = staging_dir / rel_path
            target_file = install_dir / rel_path
            if staged_file.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_file, target_file)
                applied += 1

        logger.info(f"✨ [Update Applied] Installed {applied} files ({version_str})")
        return True, f"Successfully applied update ({applied} files)."
    except Exception as e:
        return False, f"Error applying update to installation directory: {e}"


def rollback_to_lkg(backup_dir: Path, install_dir: Path) -> Tuple[bool, str]:
    """Rolls back the application installation directory to the Last-Known-Good snapshot."""
    try:
        if not backup_dir.exists():
            return False, f"LKG backup directory '{backup_dir}' not found."

        restored = 0
        for p in backup_dir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(backup_dir)
                dest = install_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                restored += 1

        logger.critical(f"🛡️ [ROLLBACK COMPLETED] Restored {restored} files from LKG backup '{backup_dir.name}'")
        return True, f"Successfully rolled back {restored} files from LKG backup."
    except Exception as e:
        return False, f"Error during LKG rollback: {e}"


# -----------------------------------------------------------------------------
# 4. HEALTH CHECKING & WATCHDOG RECOVERY
# -----------------------------------------------------------------------------

def check_backend_health_http(
    url: str = "http://127.0.0.1:8000/api/health",
    timeout_sec: float = 2.0
) -> bool:
    """Verifies that the Chirkoot backend is listening and returning HTTP 200 with status 'ok'."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ChirkootHealthCheck/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
                return data.get("status") == "ok"
        return False
    except Exception:
        return False


def wait_for_backend_health(
    url: str = "http://127.0.0.1:8000/api/health",
    max_wait_seconds: float = 12.0,
    poll_interval: float = 0.25
) -> bool:
    """Polls backend health check until ready or timeout expires."""
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        if check_backend_health_http(url=url, timeout_sec=0.8):
            return True
        time.sleep(poll_interval)
    return False


# -----------------------------------------------------------------------------
# 5. WIN32 SINGLE-INSTANCE MUTEX & FOREGROUND ACTIVATION
# -----------------------------------------------------------------------------

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

def acquire_single_instance_mutex(mutex_name: str = "Local\\Chirkoot_MKV_Application_Mutex") -> Tuple[bool, Any]:
    """
    Enforces single-instance execution via Windows named mutex.
    Returns (True, handle) if primary instance, or (False, handle) if another instance is already running.
    """
    if not kernel32:
        return True, None

    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    last_err = kernel32.GetLastError()
    if last_err == ERROR_ALREADY_EXISTS:
        return False, mutex
    return True, mutex


def release_single_instance_mutex(mutex_handle: Any):
    """Releases the Windows named mutex upon graceful exit."""
    if kernel32 and mutex_handle:
        kernel32.ReleaseMutex(mutex_handle)
        kernel32.CloseHandle(mutex_handle)


def bring_existing_instance_to_foreground(title_substring: str = "CHIRKOOT") -> bool:
    """Finds the window of an existing Chirkoot instance and activates it."""
    if not user32:
        return False

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    found_hwnds = []

    def enum_cb(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                if title_substring.lower() in buff.value.lower():
                    found_hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    if found_hwnds:
        h = found_hwnds[0]
        user32.ShowWindow(h, SW_RESTORE)
        user32.SetForegroundWindow(h)
        return True
    return False


# -----------------------------------------------------------------------------
# 6. UNINSTALLATION & FRESH INSTALLATION UTILITIES
# -----------------------------------------------------------------------------

def uninstall_chirkoot(install_dir: Path, remove_user_data: bool = False) -> Tuple[bool, str]:
    """
    Uninstalls Chirkoot application files.
    By default, preserves user data in %LOCALAPPDATA%\\Chirkoot unless explicitly requested.
    """
    try:
        # Delete core files inside install_dir
        for p in install_dir.glob("*"):
            if p.name != ".git":
                if p.is_file():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)

        if remove_user_data and USER_DATA_DIR.exists():
            shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
            msg = "Uninstalled Chirkoot and purged all user data."
        else:
            msg = "Uninstalled Chirkoot application. User data preserved in %LOCALAPPDATA%\\Chirkoot."

        logger.info(f"🗑️ [Uninstall] {msg}")
        return True, msg
    except Exception as e:
        return False, f"Error during uninstallation: {e}"
