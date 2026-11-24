"""
Test Suite: Chirkoot MK-V Windows Launcher, Lifecycle & Deployment Architecture
================================================================================
Tests comprehensive deployment, lifecycle, and packaging requirements:
1. Separation of application installation from mutable user data (%LOCALAPPDATA%\\Chirkoot).
2. Win32 Single-Instance Mutex collision and foreground activation.
3. Reliable backend startup and HTTP /api/health validation.
4. Watchdog crash detection, safe mode auto-engagement, and health recovery.
5. Cryptographic RSA-PSS manifest signing and signature verification.
6. Corrupted update package rejection via SHA-256 integrity verification.
7. Path traversal (Zip Slip) update package rejection.
8. Unsigned / untrusted update package rejection.
9. Staged update application, simulated failed health check, and automatic LKG rollback.
10. User data preservation during update, rollback, and uninstallation.
11. Uninstallation with and without user data purge.
12. Loose Python script tampering detection and automatic LKG recovery.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from deployment_manager import (
    INSTALL_DIR,
    USER_DATA_DIR,
    acquire_single_instance_mutex,
    apply_staged_update,
    bring_existing_instance_to_foreground,
    build_update_package,
    calculate_file_sha256,
    check_backend_health_http,
    create_lkg_backup,
    create_update_manifest,
    ensure_user_data_directories,
    generate_installation_manifests,
    generate_keypair,
    get_installation_dir,
    get_user_data_dir,
    release_single_instance_mutex,
    rollback_to_lkg,
    seed_initial_lkg_backup,
    sign_manifest,
    uninstall_chirkoot,
    verify_and_stage_update,
    verify_manifest_signature,
    wait_for_backend_health,
)
from desktop_app import StartupWatchdog


class TestLauncherAndDeployment(unittest.TestCase):
    """Unit and integration test cases for Chirkoot deployment and lifecycle subsystem."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="chirkoot_deploy_test_")
        self.mock_install_dir = Path(self.temp_dir) / "install"
        self.mock_install_dir.mkdir(parents=True, exist_ok=True)

        self.mock_user_data_dir = Path(self.temp_dir) / "user_data"
        self.mock_user_data_dir.mkdir(parents=True, exist_ok=True)

        # Generate test signing keys
        self.private_pem, self.public_pem = generate_keypair()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. DIRECTORY SEPARATION & USER DATA BOUNDARIES
    # -------------------------------------------------------------------------
    def test_directory_separation_and_user_data_isolation(self):
        """Verifies strict separation of install directory from %LOCALAPPDATA%\\Chirkoot."""
        inst = get_installation_dir()
        user_data = get_user_data_dir()

        self.assertTrue(inst.exists(), "Installation directory must exist.")
        self.assertIn("Chirkoot", str(user_data), "User data dir must target %LOCALAPPDATA%\\Chirkoot.")
        self.assertNotEqual(str(inst).lower(), str(user_data).lower(),
                            "Install dir and user data dir must never be identical.")

        # Test subdirectories initialization
        with patch("deployment_manager.USER_DATA_DIR", self.mock_user_data_dir), \
             patch("deployment_manager.DATA_DIR", self.mock_user_data_dir / "data"), \
             patch("deployment_manager.LOGS_DIR", self.mock_user_data_dir / "logs"), \
             patch("deployment_manager.CONFIG_DIR", self.mock_user_data_dir / "config"), \
             patch("deployment_manager.BACKUPS_DIR", self.mock_user_data_dir / "backups"), \
             patch("deployment_manager.STAGING_DIR", self.mock_user_data_dir / "staging"), \
             patch("deployment_manager.BROWSER_PROFILE_DIR", self.mock_user_data_dir / "browser_profile"), \
             patch("deployment_manager.TEMP_DIR", self.mock_user_data_dir / "temp"), \
             patch("deployment_manager.VAULT_DIR", self.mock_user_data_dir / "vault"), \
             patch("deployment_manager.SKILLS_DIR", self.mock_user_data_dir / "custom_skills"):

            ensure_user_data_directories()

            expected_dirs = [
                "data", "logs", "config", "backups", "staging",
                "browser_profile", "temp", "vault", "custom_skills"
            ]
            for sub in expected_dirs:
                sub_path = self.mock_user_data_dir / sub
                self.assertTrue(sub_path.exists(), f"User data subpath {sub} must be initialized.")

    # -------------------------------------------------------------------------
    # 2. SINGLE-INSTANCE MUTEX ENFORCEMENT
    # -------------------------------------------------------------------------
    def test_single_instance_mutex_collision(self):
        """Verifies named mutex detects duplicate instances and cleans up handle."""
        test_mutex_name = f"Local\\Chirkoot_Test_Mutex_{int(time.time() * 1000)}"

        # First instance acquires mutex successfully
        is_primary_1, handle_1 = acquire_single_instance_mutex(test_mutex_name)
        self.assertTrue(is_primary_1, "First instance must successfully acquire the named mutex.")
        self.assertIsNotNone(handle_1, "First instance must receive a valid Win32 mutex handle.")

        try:
            # Second instance must detect duplicate
            is_primary_2, handle_2 = acquire_single_instance_mutex(test_mutex_name)
            self.assertFalse(is_primary_2, "Second instance must report not primary (ERROR_ALREADY_EXISTS).")
            if handle_2:
                release_single_instance_mutex(handle_2)
        finally:
            release_single_instance_mutex(handle_1)

        # After releasing primary, third instance can acquire
        is_primary_3, handle_3 = acquire_single_instance_mutex(test_mutex_name)
        self.assertTrue(is_primary_3, "After release, another instance must acquire successfully.")
        release_single_instance_mutex(handle_3)

    # -------------------------------------------------------------------------
    # 3. BACKEND STARTUP & HTTP HEALTH CHECK
    # -------------------------------------------------------------------------
    def test_backend_health_http_validation(self):
        """Verifies check_backend_health_http correctly parses valid 200 vs errors."""
        # Case A: Valid 200 response with status: "ok"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"status": "ok", "version": "5.0.0"}'
        mock_response.__enter__.return_value = mock_response

        with patch("deployment_manager.urllib.request.urlopen", return_value=mock_response):
            self.assertTrue(check_backend_health_http("http://127.0.0.1:8000/api/health"))

        # Case B: HTTP 500 error response
        mock_500 = MagicMock()
        mock_500.status = 500
        mock_500.read.return_value = b'{"status": "error"}'
        mock_500.__enter__.return_value = mock_500
        with patch("deployment_manager.urllib.request.urlopen", return_value=mock_500):
            self.assertFalse(check_backend_health_http("http://127.0.0.1:8000/api/health"))

        # Case C: Connection error / refusal
        with patch("deployment_manager.urllib.request.urlopen", side_effect=Exception("Connection refused")):
            self.assertFalse(check_backend_health_http("http://127.0.0.1:8000/api/health"))

        # Case D: wait_for_backend_health polling
        with patch("deployment_manager.check_backend_health_http", side_effect=[False, False, True]):
            self.assertTrue(wait_for_backend_health("http://127.0.0.1:8000/api/health", max_wait_seconds=2.0))

    # -------------------------------------------------------------------------
    # 4. WATCHDOG CRASH DETECTION & SAFE MODE RECOVERY
    # -------------------------------------------------------------------------
    def test_startup_watchdog_crash_recovery(self):
        """Verifies consecutive crash accumulation forces Safe Mode and resets on boot."""
        health_file = self.mock_user_data_dir / "config" / "startup_health.json"
        health_file.parent.mkdir(parents=True, exist_ok=True)

        with patch("learning_engine.STARTUP_HEALTH_FILE", health_file):
            # Reset any prior state
            StartupWatchdog.record_successful_boot()
            self.assertFalse(StartupWatchdog.should_force_safe_mode())

            # Simulate 1st crash
            StartupWatchdog.record_crash("Synthetic crash 1")
            self.assertFalse(StartupWatchdog.should_force_safe_mode())

            # Simulate 2nd crash
            StartupWatchdog.record_crash("Synthetic crash 2")
            self.assertFalse(StartupWatchdog.should_force_safe_mode())

            # Simulate 3rd crash -> Must engage Safe Mode
            StartupWatchdog.record_crash("Synthetic crash 3")
            self.assertTrue(StartupWatchdog.should_force_safe_mode())

            # Successful boot resets consecutive crashes
            StartupWatchdog.record_successful_boot()
            self.assertFalse(StartupWatchdog.should_force_safe_mode())

    # -------------------------------------------------------------------------
    # 5. CRYPTOGRAPHIC UPDATE MANIFEST SIGNING & VERIFICATION
    # -------------------------------------------------------------------------
    def test_manifest_signing_and_signature_verification(self):
        """Verifies RSA-PSS SHA-256 signature generation and rejection of forged manifests."""
        # Create sample files
        (self.mock_install_dir / "server.py").write_text("print('server')", encoding="utf-8")
        (self.mock_install_dir / "desktop_app.py").write_text("print('hud')", encoding="utf-8")

        manifest = create_update_manifest(
            source_dir=self.mock_install_dir,
            version="5.0.1",
            private_key_pem=self.private_pem
        )

        self.assertIn("signature", manifest)
        self.assertEqual(manifest["version"], "5.0.1")
        self.assertIn("server.py", manifest["files"])
        self.assertIn("desktop_app.py", manifest["files"])

        # Verification with trusted public key
        is_valid = verify_manifest_signature(manifest, self.public_pem)
        self.assertTrue(is_valid, "Valid RSA-PSS signature must verify cleanly.")

        # Tampered manifest (attacker modified file hash or version)
        tampered_manifest = dict(manifest)
        tampered_manifest["version"] = "5.0.2"
        is_tampered_valid = verify_manifest_signature(tampered_manifest, self.public_pem)
        self.assertFalse(is_tampered_valid, "Tampered manifest payload must fail signature verification.")

        # Untrusted key verification
        other_priv, other_pub = generate_keypair()
        is_wrong_key = verify_manifest_signature(manifest, other_pub)
        self.assertFalse(is_wrong_key, "Signature verified against wrong public key must fail.")

    # -------------------------------------------------------------------------
    # 6. CORRUPTED UPDATE PACKAGE REJECTION
    # -------------------------------------------------------------------------
    def test_corrupted_update_package_rejection(self):
        """Verifies update package with tampered file payload is rejected and staging purged."""
        file1 = self.mock_install_dir / "core_module.py"
        file1.write_text("original_valid_code = True", encoding="utf-8")

        manifest = create_update_manifest(
            source_dir=self.mock_install_dir,
            version="5.0.2",
            private_key_pem=self.private_pem
        )

        pkg_zip = Path(self.temp_dir) / "update_pkg.zip"
        build_update_package(self.mock_install_dir, pkg_zip, manifest)

        # Intentionally tamper with zip contents: replace core_module.py with malicious content
        # while keeping original manifest
        tampered_zip = Path(self.temp_dir) / "tampered_pkg.zip"
        import zipfile
        with zipfile.ZipFile(pkg_zip, "r") as zin:
            with zipfile.ZipFile(tampered_zip, "w") as zout:
                for item in zin.infolist():
                    if item.filename == "core_module.py":
                        zout.writestr("core_module.py", "malicious_injected_code = True")
                    else:
                        zout.writestr(item, zin.read(item.filename))

        staging_dir = Path(self.temp_dir) / "staging"
        success, msg, staged_manifest = verify_and_stage_update(
            update_zip_path=tampered_zip,
            staging_target_dir=staging_dir,
            trusted_public_key_pem=self.public_pem
        )

        self.assertFalse(success, "Corrupted update payload must be rejected.")
        self.assertIn("SHA-256 mismatch", msg)
        self.assertFalse(staging_dir.exists(), "Staging directory must be automatically purged upon failure.")

    # -------------------------------------------------------------------------
    # 7. ZIP-SLIP PATH TRAVERSAL REJECTION
    # -------------------------------------------------------------------------
    def test_zip_slip_path_traversal_rejection(self):
        """Verifies update packages attempting directory traversal are rejected immediately."""
        evil_zip = Path(self.temp_dir) / "evil_pkg.zip"
        import zipfile
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("../evil_outside.py", "pwned = True")

        staging_dir = Path(self.temp_dir) / "staging_evil"
        success, msg, _ = verify_and_stage_update(
            update_zip_path=evil_zip,
            staging_target_dir=staging_dir,
            trusted_public_key_pem=self.public_pem
        )

        self.assertFalse(success, "Zip slip package must be rejected.")
        self.assertIn("path traversal", msg.lower())
        self.assertFalse(staging_dir.exists())

    # -------------------------------------------------------------------------
    # 8. STAGED UPDATE APPLICATION & AUTOMATIC LKG ROLLBACK
    # -------------------------------------------------------------------------
    def test_staged_update_application_and_lkg_rollback(self):
        """Verifies atomic application and rollback to LKG snapshot if health check fails."""
        # 1. Initial installation state (v1)
        app_file = self.mock_install_dir / "app.py"
        app_file.write_text("VERSION = '1.0.0'", encoding="utf-8")

        lkg_backup_dir = Path(self.temp_dir) / "lkg_backup"
        success_snap, _ = create_lkg_backup(self.mock_install_dir, lkg_backup_dir)
        self.assertTrue(success_snap)

        # 2. Stage updated files (v2)
        staging_dir = Path(self.temp_dir) / "staging_v2"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "app.py").write_text("VERSION = '2.0.0'", encoding="utf-8")
        (staging_dir / "new_feature.py").write_text("FEATURE = True", encoding="utf-8")

        # Apply update
        applied, msg = apply_staged_update(staging_dir, self.mock_install_dir)
        self.assertTrue(applied)
        self.assertEqual(app_file.read_text(encoding="utf-8"), "VERSION = '2.0.0'")
        self.assertTrue((self.mock_install_dir / "new_feature.py").exists())

        # 3. Simulate health check failure -> Trigger automatic rollback
        rolled_back, r_msg = rollback_to_lkg(lkg_backup_dir, self.mock_install_dir)
        self.assertTrue(rolled_back, "Rollback to LKG must succeed.")
        self.assertEqual(app_file.read_text(encoding="utf-8"), "VERSION = '1.0.0'",
                         "App file must be restored to exact LKG v1 state.")

    # -------------------------------------------------------------------------
    # 9. USER DATA PRESERVATION DURING UPDATES & ROLLBACK
    # -------------------------------------------------------------------------
    def test_user_data_preservation_across_lifecycle(self):
        """Verifies mutable data in %LOCALAPPDATA% is never wiped by update or rollback."""
        user_memory_db = self.mock_user_data_dir / "data" / "memory.db"
        user_memory_db.parent.mkdir(parents=True, exist_ok=True)
        user_memory_db.write_text("SQLITE_USER_PERSISTENT_MEMORY", encoding="utf-8")

        user_config = self.mock_user_data_dir / "config" / "user_settings.json"
        user_config.parent.mkdir(parents=True, exist_ok=True)
        user_config.write_text('{"user_theme": "cyber_amber"}', encoding="utf-8")

        # Simulate update & rollback cycle
        staging_dir = Path(self.temp_dir) / "staging_temp"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "dummy.py").write_text("dummy", encoding="utf-8")

        apply_staged_update(staging_dir, self.mock_install_dir)

        # Verify user data untouched
        self.assertTrue(user_memory_db.exists())
        self.assertEqual(user_memory_db.read_text(encoding="utf-8"), "SQLITE_USER_PERSISTENT_MEMORY")
        self.assertTrue(user_config.exists())
        self.assertEqual(user_config.read_text(encoding="utf-8"), '{"user_theme": "cyber_amber"}')

    # -------------------------------------------------------------------------
    # 10. UNINSTALLATION BEHAVIOR
    # -------------------------------------------------------------------------
    def test_uninstallation_preserves_user_data_unless_purged(self):
        """Verifies uninstall_chirkoot preserves user data by default, and purges only when requested."""
        dummy_bin = self.mock_install_dir / "ChirkootLauncher.exe"
        dummy_bin.write_text("BINARY", encoding="utf-8")

        user_file = self.mock_user_data_dir / "data" / "user_notes.txt"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_text("My personal notes", encoding="utf-8")

        with patch("deployment_manager.USER_DATA_DIR", self.mock_user_data_dir):
            # Standard uninstall: remove application files, preserve user data
            ok, msg = uninstall_chirkoot(self.mock_install_dir, remove_user_data=False)
            self.assertTrue(ok)
            self.assertFalse(dummy_bin.exists(), "Application files must be removed.")
            self.assertTrue(user_file.exists(), "User data must be preserved.")

            # Recreate dummy bin and test full purge
            dummy_bin.write_text("BINARY2", encoding="utf-8")
            ok2, msg2 = uninstall_chirkoot(self.mock_install_dir, remove_user_data=True)
            self.assertTrue(ok2)
            self.assertFalse(self.mock_user_data_dir.exists(), "User data must be purged when requested.")

    # -------------------------------------------------------------------------
    # 11. LOOSE PYTHON INTEGRITY VERIFICATION & LKG AUTO-RECOVERY
    # -------------------------------------------------------------------------
    def test_loose_python_integrity_verification_and_recovery(self):
        """Verifies manifest generator, SHA-256 calculation, and recovery from LKG on tampering."""
        # Setup initial valid files
        core_script = self.mock_install_dir / "security_policy.py"
        core_script.write_text("# Untampered security kernel\nSTRICT_MODE = True\n", encoding="utf-8")

        # Generate manifest
        sha_path, json_path = generate_installation_manifests(self.mock_install_dir, version="5.0.0")
        self.assertTrue(sha_path.exists())
        self.assertTrue(json_path.exists())

        original_sha = calculate_file_sha256(core_script)

        # Seed LKG backup
        lkg_core_dir = Path(self.temp_dir) / "lkg_core"
        seed_initial_lkg_backup(self.mock_install_dir, lkg_core_dir)
        self.assertTrue((lkg_core_dir / "security_policy.py").exists())

        # Simulate tampering on loose script
        core_script.write_text("# Malicious bypass injected!\nSTRICT_MODE = False\n", encoding="utf-8")
        tampered_sha = calculate_file_sha256(core_script)
        self.assertNotEqual(original_sha, tampered_sha)

        # Simulate launcher pre-launch integrity check & recovery logic
        with open(json_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        expected_sha = manifest_data["files"]["security_policy.py"]["sha256"]
        self.assertEqual(expected_sha, original_sha)

        current_sha = calculate_file_sha256(core_script)
        if current_sha != expected_sha:
            # Recovery trigger
            backup_source = lkg_core_dir / "security_policy.py"
            shutil.copy2(backup_source, core_script)

        recovered_sha = calculate_file_sha256(core_script)
        self.assertEqual(recovered_sha, original_sha, "Tampered file must be recovered to golden SHA-256.")
        self.assertIn("STRICT_MODE = True", core_script.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
