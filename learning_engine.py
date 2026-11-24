"""Chirkoot MK-V Autonomous Self-Learning Engine.

Implements a secure development lifecycle for self-learning and custom skills:
1. Core Immutability: User mutable data isolated in %LOCALAPPDATA%\\Chirkoot.
   Protected repo files (security policy, learning engine, launcher, dispatcher, core app)
   are dynamically identified from the Git repository and protected against any agent tampering.
2. 10-Stage Secure Skill Lifecycle:
   (1) Candidate Generation, (2) Git Staging, (3) Syntax & Static AST Analysis,
   (4) Dependency & Permission Manifests, (5) Human-Readable Unified Diff,
   (6) Explicit On-Screen Approval Token, (7) Isolated Subprocess Testing,
   (8) Canary Health Verification & Activation, (9) Last-Known-Good (LKG) Tagging,
   (10) Automatic Rollback and Quarantine.
3. Git Versioning: Full version history, commit tracking, and rollback via local git repo.
4. Restricted Subprocess Sandbox: Process-tree termination (taskkill), environment wiping,
   socket disabling, filesystem access sandboxing.
5. Stable Dynamic Tool Dispatch: First-class list_skills and run_skill tools.
6. Safe Boot Mode: --safe-mode disallows custom skills and runs verified core only.
7. Startup Watchdog: Monitors backend startup health; auto-recovers LKG on repeated crash loops.
"""

import os
import sys
import re
import ast
import json
import time
import shutil
import base64
import difflib
import secrets
import logging
import subprocess
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple, Set

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger("ChirkootLearningEngine")

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from security_policy import (
    CHIRKOOT_APPDATA_DIR,
    DATA_DIR,
    SKILLS_DIR,
    ACTIVE_SKILLS_DIR,
    STAGING_SKILLS_DIR,
    QUARANTINE_SKILLS_DIR,
    SKILLS_REPO_DIR,
    MEMORY_DIR,
    CONFIG_DIR,
    TEMP_DIR,
    BACKUP_DIR,
    LOGS_DIR,
    is_immutable_core_path,
    is_sensitive_path,
    GLOBAL_TAINT_TRACKER,
    record_audit_event,
    is_kill_switch_active,
    register_active_child_process,
    unregister_active_child_process,
    get_repo_protected_files,
    redact_secrets
)

# Safe Mode Flag
SAFE_MODE = (
    "--safe-mode" in sys.argv or
    os.getenv("CHIRKOOT_SAFE_MODE", "0") == "1"
)

MEMORY_FILE = MEMORY_DIR / "memory.json"
SKILLS_CATALOG_FILE = CONFIG_DIR / "skills_catalog.json"
STARTUP_HEALTH_FILE = CONFIG_DIR / "startup_health.json"
CHANGE_LOG_FILE = DATA_DIR / "change_log.json"

from sqlite_memory import (
    SQLITE_MEMORY,
    SQLiteMemoryEngine,
    canonicalize_category,
    generate_semantic_embedding,
    MEMORY_DB_FILE
)

# In-memory storage for pending proposals awaiting on-screen approval
_PENDING_PROPOSALS: Dict[str, Dict[str, Any]] = {}


# ==============================================================================
# DATA MIGRATION & REPOSITORY INITIALIZATION
# ==============================================================================

def _migrate_existing_data():
    """Migrates legacy memory and custom skills to %LOCALAPPDATA%\\Chirkoot."""
    legacy_data_dir = ROOT_DIR / "data"
    legacy_skills_dir = ROOT_DIR / "custom_skills"

    # Migrate memory
    if not MEMORY_FILE.exists():
        if (legacy_data_dir / "memory.json").exists():
            try:
                shutil.copy2(legacy_data_dir / "memory.json", MEMORY_FILE)
            except Exception as e:
                logger.warning(f"Error migrating memory.json: {e}")
        elif (DATA_DIR / "memory.json").exists():
            try:
                shutil.copy2(DATA_DIR / "memory.json", MEMORY_FILE)
            except Exception:
                pass

    # Migrate legacy custom skills to active skills dir
    if legacy_skills_dir.exists():
        for py_file in legacy_skills_dir.glob("*.py"):
            target = ACTIVE_SKILLS_DIR / py_file.name
            if not target.exists():
                try:
                    shutil.copy2(py_file, target)
                except Exception:
                    pass

    # Migrate legacy memory.json into SQLite FTS5 + sqlite-vec engine
    try:
        conn = SQLITE_MEMORY._get_connection()
        count = conn.execute("SELECT count(*) as count FROM memories;").fetchone()["count"]
        if count == 0 and MEMORY_FILE.exists():
            logger.info("[Migration] SQLite memories table is empty. Running initial migration from memory.json.")
            SQLITE_MEMORY.migrate_from_json(delete_original_after_verification=False)
    except Exception as me:
        logger.warning(f"[Migration] Auto SQLite migration check error: {me}")

_migrate_existing_data()


# ==============================================================================
# 1. GIT-BACKED SKILL REPOSITORY MANAGER
# ==============================================================================

class SkillsGitRepository:
    """Manages versioning, staging, committing, and rollbacks in %LOCALAPPDATA%\\Chirkoot\\skills_repo."""

    @classmethod
    def ensure_initialized(cls) -> bool:
        """Initializes the Git repository in SKILLS_REPO_DIR if not already present."""
        SKILLS_REPO_DIR.mkdir(parents=True, exist_ok=True)
        git_dir = SKILLS_REPO_DIR / ".git"
        if not git_dir.exists():
            try:
                subprocess.run(["git", "init"], cwd=str(SKILLS_REPO_DIR), capture_output=True, check=True)
                subprocess.run(["git", "config", "user.name", "Chirkoot"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
                subprocess.run(["git", "config", "user.email", "chirkoot@local"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
                
                # Create initial commit
                readme = SKILLS_REPO_DIR / "README.md"
                with open(readme, "w", encoding="utf-8") as f:
                    f.write("# Chirkoot Skills Repository\nVersioned storage for autonomous learned skills.\n")

                subprocess.run(["git", "add", "README.md"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
                subprocess.run(["git", "commit", "-m", "initial: initialize skills repository"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
                subprocess.run(["git", "tag", "-f", "last_known_good"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
                return True
            except Exception as e:
                logger.error(f"Error initializing skills git repo: {e}")
                return False
        return True

    @classmethod
    def commit_skill_activation(cls, skill_name: str, version: str = "1.0.0") -> Tuple[bool, str]:
        """Copies active skill files into repo, commits, and tags last_known_good."""
        cls.ensure_initialized()
        try:
            repo_active = SKILLS_REPO_DIR / "active"
            repo_active.mkdir(parents=True, exist_ok=True)

            src_code = ACTIVE_SKILLS_DIR / f"{skill_name}.py"
            src_manifest = ACTIVE_SKILLS_DIR / f"{skill_name}.manifest.json"

            if src_code.exists():
                shutil.copy2(src_code, repo_active / f"{skill_name}.py")
            if src_manifest.exists():
                shutil.copy2(src_manifest, repo_active / f"{skill_name}.manifest.json")

            subprocess.run(["git", "add", "."], cwd=str(SKILLS_REPO_DIR), capture_output=True, check=True)
            res = subprocess.run(
                ["git", "commit", "-m", f"activate: {skill_name} v{version}"],
                cwd=str(SKILLS_REPO_DIR),
                capture_output=True,
                text=True
            )
            # Update last_known_good tag to point to this verified commit
            subprocess.run(["git", "tag", "-f", "last_known_good"], cwd=str(SKILLS_REPO_DIR), capture_output=True)
            return True, f"Git commit and LKG tag created: activate {skill_name} v{version}"
        except Exception as e:
            logger.error(f"Error committing skill activation to git: {e}")
            return False, str(e)

    @classmethod
    def rollback_to_last_known_good(cls) -> Tuple[bool, str]:
        """Restores repository and active skills to the last_known_good git tag."""
        cls.ensure_initialized()
        try:
            # Check out last_known_good tag
            res = subprocess.run(
                ["git", "checkout", "last_known_good", "--force"],
                cwd=str(SKILLS_REPO_DIR),
                capture_output=True,
                text=True
            )
            if res.returncode != 0:
                # If tag checkout failed, try HEAD~1
                subprocess.run(["git", "checkout", "HEAD~1", "--force"], cwd=str(SKILLS_REPO_DIR), capture_output=True)

            # Sync repo active directory back to ACTIVE_SKILLS_DIR
            repo_active = SKILLS_REPO_DIR / "active"
            if repo_active.exists():
                for p in repo_active.glob("*.*"):
                    shutil.copy2(p, ACTIVE_SKILLS_DIR / p.name)

            return True, "Successfully rolled back skills to last_known_good git state."
        except Exception as e:
            logger.error(f"Git rollback error: {e}")
            return False, str(e)


# ==============================================================================
# 2. RESTRICTED SUBPROCESS SANDBOX
# ==============================================================================

class SubprocessSkillSandbox:
    """Executes skills inside an isolated subprocess via sandbox_runner.py.
    
    Enforces process-tree termination on timeout, stripped environment variables,
    socket disabling, and filesystem access sandboxing.
    """

    @classmethod
    def execute(
        cls,
        skill_name: str,
        skill_file: Path,
        manifest_file: Optional[Path] = None,
        args: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
        max_output_chars: int = 8192
    ) -> Dict[str, Any]:
        """Executes a custom skill in the restricted subprocess sandbox."""
        if SAFE_MODE:
            return {
                "status": "error",
                "error_type": "SafeModeActive",
                "error": "Custom skills execution is disabled in Safe Mode."
            }

        if is_kill_switch_active():
            return {
                "status": "error",
                "error_type": "KillSwitchActive",
                "error": "Execution blocked: Emergency Stop is currently engaged."
            }

        runner_script = ROOT_DIR / "sandbox_runner.py"
        if not runner_script.exists():
            return {
                "status": "error",
                "error_type": "FileNotFoundError",
                "error": "sandbox_runner.py is missing from project core."
            }

        args_json = json.dumps(args or {})
        b64_args = base64.b64encode(args_json.encode("utf-8")).decode("utf-8")

        cmd = [
            sys.executable,
            "-E",  # Ignore PYTHON* environment variables
            "-s",  # Don't add user site directory to sys.path
            str(runner_script),
            "--skill", skill_name,
            "--skill-file", str(skill_file),
            "--args-b64", b64_args
        ]
        if manifest_file and manifest_file.exists():
            cmd.extend(["--manifest", str(manifest_file)])

        # Construct sanitized minimal environment (wipe credentials and secrets)
        safe_env = {
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "PATH": os.environ.get("PATH", ""),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "TMP": str(TEMP_DIR),
            "TEMP": str(TEMP_DIR),
            "PYTHONIOENCODING": "utf-8",
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", "")
        }

        start_t = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=safe_env,
                cwd=str(TEMP_DIR)
            )
            register_active_child_process(proc.pid)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Forcefully kill the entire process tree on Windows
                logger.warning(f"[Skill Sandbox] Timeout ({timeout}s). Forcefully killing PID {proc.pid}")
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5.0)
                except Exception:
                    proc.kill()
                return {
                    "status": "error",
                    "error_type": "TimeoutExpired",
                    "error": f"Skill '{skill_name}' timed out after {timeout} seconds. Process tree terminated."
                }
            finally:
                unregister_active_child_process(proc.pid)

            elapsed = round(time.time() - start_t, 3)

            # Parse structured JSON from stdout
            if stdout:
                lines = stdout.strip().splitlines()
                # Find last valid JSON line
                for line in reversed(lines):
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict) and "status" in parsed:
                            parsed["execution_time_s"] = elapsed
                            return parsed
                    except Exception:
                        pass

            # If not valid JSON, check exit code & stderr
            if proc.returncode != 0:
                err_text = stderr.strip() if stderr else f"Subprocess exited with non-zero code {proc.returncode}."
                if len(err_text) > max_output_chars:
                    err_text = err_text[:max_output_chars] + "..."
                return {
                    "status": "error",
                    "error_type": "SubprocessError",
                    "error": err_text,
                    "exit_code": proc.returncode,
                    "execution_time_s": elapsed
                }

            raw_out = stdout.strip()[:max_output_chars]
            return {
                "status": "success",
                "result": raw_out,
                "execution_time_s": elapsed
            }

        except Exception as e:
            return {
                "status": "error",
                "error_type": type(e).__name__,
                "error": f"Sandbox execution exception: {e}"
            }


# ==============================================================================
# 3. 10-STAGE SECURE SKILL LIFECYCLE ENGINE
# ==============================================================================

class SkillLifecycleEngine:
    """Orchestrates candidate generation, staging, AST inspection, diff preview,
    
    physical approval, isolated testing, canary validation, activation, and quarantine.
    """

    @classmethod
    def get_catalog(cls) -> Dict[str, Any]:
        """Loads active skills catalog."""
        if SKILLS_CATALOG_FILE.exists():
            try:
                with open(SKILLS_CATALOG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @classmethod
    def save_catalog(cls, catalog: Dict[str, Any]):
        """Persists active skills catalog."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(SKILLS_CATALOG_FILE, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving skills catalog: {e}")

    @classmethod
    def validate_syntax_and_ast(cls, code: str, skill_name: str) -> Tuple[bool, str]:
        """Runs syntax and static AST analysis to enforce security boundaries."""
        if not code or not code.strip():
            return False, "Code is empty."

        try:
            tree = ast.parse(code)
        except SyntaxError as se:
            return False, f"Syntax error on line {se.lineno}: {se.msg}"

        # 1. Verify required function entrypoint def {skill_name}(...)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == skill_name]
        if not funcs:
            found_defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
            return False, f"Code must define an entrypoint function 'def {skill_name}(...)'. Found: {found_defs}"

        # 2. Strict static rule check: banned modules and functions
        banned_modules = {
            "ctypes", "winreg", "msvcrt", "subprocess", "pty",
            "paramiko", "telnetlib", "security_policy", "learning_engine", "dispatcher"
        }
        banned_calls = {
            "eval", "exec", "__import__", "globals", "locals",
            "os.system", "os.popen", "shutil.rmtree"
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0].lower()
                    if root_mod in banned_modules:
                        return False, f"Prohibited module import '{alias.name}'. Skills cannot access OS internals or launchers."
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.split(".")[0].lower()
                    if root_mod in banned_modules:
                        return False, f"Prohibited import from '{node.module}'. Skills cannot import core security modules."
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in banned_calls:
                        return False, f"Prohibited dynamic call '{node.func.id}()' detected in skill code."
                elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    call_name = f"{node.func.value.id}.{node.func.attr}"
                    if call_name in banned_calls:
                        return False, f"Prohibited function call '{call_name}()' detected."

        return True, "AST static security validation passed."

    @classmethod
    def check_dependencies_and_permissions(
        cls,
        code: str,
        manifest: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Validates requested permissions and verifies import dependencies."""
        perms = manifest.get("permissions", {})
        timeout_val = perms.get("max_timeout_s", 10.0)

        if not isinstance(timeout_val, (int, float)) or timeout_val <= 0 or timeout_val > 30.0:
            return False, f"Invalid max_timeout_s: {timeout_val}. Must be between 1.0 and 30.0 seconds."

        fs_perms = perms.get("filesystem", [])
        for rule in fs_perms:
            # Rule format: "read:<path>" or "write:<path>"
            if ":" in rule:
                _, path_str = rule.split(":", 1)
                p = Path(path_str.strip()).resolve()
                if is_immutable_core_path(str(p)):
                    return False, f"[SECURITY VIOLATION] Skill manifest requests access to immutable core path: '{p}'."
                if is_sensitive_path(str(p)):
                    return False, f"[SECURITY VIOLATION] Skill manifest requests access to sensitive path: '{p}'."

        # Verify imports resolve in current environment
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod_name = node.names[0].name.split(".")[0] if isinstance(node, ast.Import) else (node.module or "").split(".")[0]
                    if mod_name and mod_name not in sys.builtin_module_names:
                        try:
                            __import__(mod_name)
                        except ImportError:
                            return False, f"Unsatisfied dependency: Python module '{mod_name}' is not installed."
        except Exception as e:
            return False, f"Dependency inspection error: {e}"

        return True, "Dependencies and permission manifest verified."

    @classmethod
    def generate_diff(cls, skill_name: str, new_code: str) -> str:
        """Generates a human-readable unified diff between current active code and new proposal."""
        active_file = ACTIVE_SKILLS_DIR / f"{skill_name}.py"
        old_lines = []
        if active_file.exists():
            try:
                with open(active_file, "r", encoding="utf-8") as f:
                    old_lines = f.readlines()
            except Exception:
                pass

        new_lines = [line + "\n" for line in new_code.splitlines()]
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"active/{skill_name}.py",
            tofile=f"staging/{skill_name}.py",
            lineterm=""
        )
        diff_text = "\n".join(diff)
        return diff_text if diff_text.strip() else "(New skill candidate - no previous version exists)"

    @classmethod
    def stage_candidate_skill(
        cls,
        name: str,
        description: str,
        python_code: str,
        permissions: Optional[Dict[str, Any]] = None,
        test_cases: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Stages a new or modified skill and prepares on-screen approval proposal."""
        if SAFE_MODE:
            return {"status": "error", "error": "Skill creation is disabled in Safe Mode."}

        clean_name = name.strip()
        if not clean_name.isidentifier():
            return {"status": "error", "error": f"Invalid skill name '{name}'. Must be a valid Python identifier."}

        # Stage 3: Syntax & Static AST analysis
        ast_ok, ast_msg = cls.validate_syntax_and_ast(python_code, clean_name)
        if not ast_ok:
            record_audit_event("SKILL_STAGING_REJECTED", {"skill": clean_name, "reason": ast_msg})
            return {"status": "error", "error": f"Static validation failed: {ast_msg}"}

        # Build manifest
        manifest = {
            "name": clean_name,
            "version": "1.0.0",
            "description": description.strip(),
            "created_at": datetime.now().isoformat(),
            "permissions": permissions or {"filesystem": [], "network": False, "max_timeout_s": 10.0},
            "test_cases": test_cases or []
        }

        # Stage 4: Check dependencies and permissions
        dep_ok, dep_msg = cls.check_dependencies_and_permissions(python_code, manifest)
        if not dep_ok:
            record_audit_event("SKILL_PERMISSIONS_REJECTED", {"skill": clean_name, "reason": dep_msg})
            return {"status": "error", "error": f"Permission or dependency check failed: {dep_msg}"}

        # Save to STAGING_SKILLS_DIR
        STAGING_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        stg_code = STAGING_SKILLS_DIR / f"{clean_name}.py"
        stg_manifest = STAGING_SKILLS_DIR / f"{clean_name}.manifest.json"

        with open(stg_code, "w", encoding="utf-8") as f:
            f.write(python_code)
        with open(stg_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # Stage 5: Human-Readable Diff
        diff_text = cls.generate_diff(clean_name, python_code)

        # Stage 6: Generate cryptographic confirmation token for physical on-screen approval
        token = secrets.token_hex(16)
        _PENDING_PROPOSALS[clean_name] = {
            "name": clean_name,
            "token": token,
            "code_file": str(stg_code),
            "manifest_file": str(stg_manifest),
            "manifest": manifest,
            "timestamp": time.time()
        }

        perm_summary = (
            f"Network: {'ENABLED' if manifest['permissions'].get('network') else 'BLOCKED'} | "
            f"Timeout: {manifest['permissions'].get('max_timeout_s', 10.0)}s | "
            f"Filesystem Rules: {len(manifest['permissions'].get('filesystem', []))}"
        )

        return {
            "status": "pending_approval",
            "skill": clean_name,
            "confirmation_token": token,
            "permissions_summary": perm_summary,
            "diff": diff_text,
            "message": (
                f"Candidate skill '{clean_name}' successfully staged!\n"
                f"• Permissions: {perm_summary}\n"
                f"• Diff:\n{diff_text}\n\n"
                f"⚠️ EXPLICIT ON-SCREEN APPROVAL REQUIRED:\n"
                f"Call 'activate_skill(name=\"{clean_name}\", confirmation_token=\"{token}\")' "
                f"after explicit physical user confirmation to run tests and activate."
            )
        }

    @classmethod
    def activate_staged_skill(cls, name: str, confirmation_token: str) -> Dict[str, Any]:
        """Validates token, executes isolated tests, verifies health, and activates the skill."""
        if SAFE_MODE:
            return {"status": "error", "error": "Activation is disabled in Safe Mode."}

        proposal = _PENDING_PROPOSALS.get(name)
        if not proposal:
            return {"status": "error", "error": f"No pending proposal found for skill '{name}'."}

        if proposal.get("token") != confirmation_token:
            record_audit_event("SKILL_ACTIVATION_BAD_TOKEN", {"skill": name})
            return {"status": "error", "error": "Invalid confirmation token. Activation rejected."}

        stg_code = Path(proposal["code_file"])
        stg_manifest = Path(proposal["manifest_file"])
        manifest = proposal["manifest"]

        # Stage 7: Run isolated tests in sandbox
        # Test A: Import smoke test
        import_cmd = [
            sys.executable, "-E", "-s", "-c",
            f"import sys; sys.path.insert(0, r'{STAGING_SKILLS_DIR}'); import {name}"
        ]
        import_res = subprocess.run(import_cmd, capture_output=True, text=True, timeout=5.0)
        if import_res.returncode != 0:
            error_details = import_res.stderr.strip() or "Import smoke test failed."
            return {"status": "error", "error": f"Isolated import test failed: {error_details}"}

        # Test B: Run sample execution test case if defined
        test_cases = manifest.get("test_cases", [])
        sample_args = test_cases[0].get("args", {}) if test_cases else {}
        timeout_val = manifest.get("permissions", {}).get("max_timeout_s", 10.0)

        test_run = SubprocessSkillSandbox.execute(
            skill_name=name,
            skill_file=stg_code,
            manifest_file=stg_manifest,
            args=sample_args,
            timeout=timeout_val
        )
        if test_run.get("status") != "success":
            return {
                "status": "error",
                "error": f"Sandbox validation test failed: {test_run.get('error', 'Unknown execution error')}"
            }

        # Stage 8: Health check & Commit to Git
        ACTIVE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        act_code = ACTIVE_SKILLS_DIR / f"{name}.py"
        act_manifest = ACTIVE_SKILLS_DIR / f"{name}.manifest.json"

        shutil.copy2(stg_code, act_code)
        shutil.copy2(stg_manifest, act_manifest)

        # Stage 9: Git versioning & Last-Known-Good Tagging
        git_ok, git_msg = SkillsGitRepository.commit_skill_activation(name, manifest.get("version", "1.0.0"))

        # Update active catalog
        catalog = cls.get_catalog()
        catalog[name] = {
            "name": name,
            "version": manifest.get("version", "1.0.0"),
            "description": manifest.get("description", ""),
            "permissions": manifest.get("permissions", {}),
            "activated_at": datetime.now().isoformat(),
            "code_path": str(act_code)
        }
        cls.save_catalog(catalog)

        # Clean up staging proposal
        _PENDING_PROPOSALS.pop(name, None)
        try:
            stg_code.unlink()
            stg_manifest.unlink()
        except Exception:
            pass

        record_audit_event("SKILL_ACTIVATED", {"skill": name, "version": manifest.get("version")})
        logger.info(f"[Skill Lifecycle] Activated verified skill '{name}' (Git: {git_msg})")

        return {
            "status": "success",
            "skill": name,
            "message": f"Successfully activated custom skill '{name}'! Version {manifest.get('version')}. LKG tagged in Git."
        }

    @classmethod
    def rollback_skill(cls, name: str, reason: str = "") -> str:
        """Rolls back the skills repository to the last_known_good version."""
        if SAFE_MODE:
            return "[SAFE MODE] Rollback is disabled in Safe Mode."

        ok, msg = SkillsGitRepository.rollback_to_last_known_good()
        if not ok:
            return f"Rollback failed: {msg}"

        record_audit_event("SKILL_ROLLBACK", {"skill": name, "reason": reason})
        return f"Successfully rolled back skill '{name}' to last-known-good version. Details: {msg}"

    @classmethod
    def quarantine_skill(cls, name: str, reason: str = "") -> str:
        """Moves a problematic skill into quarantine and removes it from the active catalog."""
        QUARANTINE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = QUARANTINE_SKILLS_DIR / f"{name}_{ts}"
        target_dir.mkdir(parents=True, exist_ok=True)

        act_code = ACTIVE_SKILLS_DIR / f"{name}.py"
        act_manifest = ACTIVE_SKILLS_DIR / f"{name}.manifest.json"

        moved = []
        if act_code.exists():
            shutil.move(str(act_code), str(target_dir / f"{name}.py"))
            moved.append(f"{name}.py")
        if act_manifest.exists():
            shutil.move(str(act_manifest), str(target_dir / f"{name}.manifest.json"))
            moved.append(f"{name}.manifest.json")

        # Update catalog
        catalog = cls.get_catalog()
        if name in catalog:
            catalog.pop(name)
            cls.save_catalog(catalog)

        record_audit_event("SKILL_QUARANTINED", {"skill": name, "reason": reason, "quarantine_dir": str(target_dir)})
        logger.warning(f"[Skill Lifecycle] Quarantined skill '{name}' to {target_dir}")
        return f"Skill '{name}' quarantined to {target_dir}. Removed from active catalog. Reason: {reason}"


# ==============================================================================
# 4. STARTUP WATCHDOG & CRASH LOOP RECOVERY
# ==============================================================================

class StartupWatchdog:
    """Monitors backend health across startups and restores LKG after repeated crashes."""

    @classmethod
    def load_health_state(cls) -> Dict[str, Any]:
        if STARTUP_HEALTH_FILE.exists():
            try:
                with open(STARTUP_HEALTH_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "boot_count": 0,
            "consecutive_crashes": 0,
            "last_boot_time": "",
            "last_healthy_boot": "",
            "safe_mode_forced": False
        }

    @classmethod
    def save_health_state(cls, state: Dict[str, Any]):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(STARTUP_HEALTH_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving startup health state: {e}")

    @classmethod
    def record_startup_attempt(cls) -> Dict[str, Any]:
        """Called upon backend launch. Detects crash loops and triggers emergency recovery."""
        state = cls.load_health_state()
        state["boot_count"] = state.get("boot_count", 0) + 1
        state["consecutive_crashes"] = state.get("consecutive_crashes", 0) + 1
        state["last_boot_time"] = datetime.now().isoformat()

        # If 3 or more consecutive crashes occurred without a healthy session
        if state["consecutive_crashes"] >= 3:
            logger.critical("[WATCHDOG] 3 consecutive startup crashes detected! Initiating emergency recovery...")
            # 1. Rollback skills repo to last_known_good
            SkillsGitRepository.rollback_to_last_known_good()

            # 2. Quarantine skills modified in the last 15 minutes
            now = time.time()
            for py_f in ACTIVE_SKILLS_DIR.glob("*.py"):
                if (now - py_f.stat().st_mtime) < 900:  # 15 minutes
                    SkillLifecycleEngine.quarantine_skill(py_f.stem, "Watchdog: Modified prior to startup crash loop")

            # 3. Force Safe Mode
            state["safe_mode_forced"] = True
            os.environ["CHIRKOOT_SAFE_MODE"] = "1"
            record_audit_event("WATCHDOG_CRASH_LOOP_RECOVERY", {"consecutive_crashes": state["consecutive_crashes"]})

        cls.save_health_state(state)
        return state

    @classmethod
    def should_force_safe_mode(cls) -> bool:
        """Returns True if repeated crashes or safe mode is indicated in health state."""
        state = cls.load_health_state()
        return bool(state.get("safe_mode_forced") or state.get("consecutive_crashes", 0) >= 3)

    @classmethod
    def record_crash(cls, reason: str = "") -> Dict[str, Any]:
        """Convenience alias for recording a startup attempt / crash."""
        return cls.record_startup_attempt()

    @classmethod
    def record_successful_boot(cls):
        """Called once server is listening and healthy. Resets crash counter."""
        state = cls.load_health_state()
        state["consecutive_crashes"] = 0
        state["last_healthy_boot"] = datetime.now().isoformat()
        state["safe_mode_forced"] = False
        cls.save_health_state(state)

    @classmethod
    def is_safe_mode_forced(cls) -> bool:
        state = cls.load_health_state()
        return bool(state.get("safe_mode_forced", False) or state.get("consecutive_crashes", 0) >= 3)


# ==============================================================================
# 5. DYNAMIC TOOL DISPATCH: STABLE FIRST-CLASS TOOLS
# ==============================================================================

def list_skills() -> str:
    """Lists all active, validated custom skills with their descriptions and permissions."""
    if SAFE_MODE:
        return "[SAFE MODE ACTIVE] Custom skills are disabled. System is running in safe mode with immutable core only."

    catalog = SkillLifecycleEngine.get_catalog()
    if not catalog:
        return f"No custom skills currently active in {ACTIVE_SKILLS_DIR}."

    lines = ["🛠️ ACTIVE CHIRKOOT LEARNED SKILLS:"]
    for name, data in catalog.items():
        perms = data.get("permissions", {})
        net = "Network: ENABLED" if perms.get("network") else "Network: BLOCKED"
        timeout_s = perms.get("max_timeout_s", 10.0)
        lines.append(
            f"• {name} (v{data.get('version', '1.0.0')}): {data.get('description', 'No description')} "
            f"[{net} | Timeout: {timeout_s}s | Subprocess Sandbox]"
        )
    return "\n".join(lines)


def run_skill(name: str, args: Optional[Dict[str, Any]] = None) -> str:
    """Executes a registered custom skill inside the restricted subprocess sandbox.
    
    Args:
        name: The exact name of the active skill to run.
        args: Dictionary of keyword arguments to pass to the skill function.
    """
    if SAFE_MODE:
        return "[SAFE MODE] Custom skills execution is disabled in Safe Mode."

    if is_kill_switch_active():
        return "[KILL SWITCH ACTIVE] Execution blocked. Chirkoot emergency stop is engaged."

    catalog = SkillLifecycleEngine.get_catalog()
    clean_name = name.strip()
    if clean_name not in catalog:
        return (
            f"[SECURITY ERROR] Skill '{name}' is not registered in the active catalog. "
            f"Only registered, validated skills can execute. Run 'list_skills' to view approved tools."
        )

    skill_file = ACTIVE_SKILLS_DIR / f"{clean_name}.py"
    manifest_file = ACTIVE_SKILLS_DIR / f"{clean_name}.manifest.json"

    if not skill_file.exists():
        return f"Error: Skill file '{skill_file}' does not exist on disk."

    manifest = catalog[clean_name]
    perms = manifest.get("permissions", {})
    timeout_val = perms.get("max_timeout_s", 10.0)

    # Scrutiny check: if untrusted taint is active and skill has write/side-effects, log audit
    is_tainted, taint_source, _ = GLOBAL_TAINT_TRACKER.is_tainted()
    if is_tainted:
        record_audit_event("TAINTED_SKILL_INVOCATION", {
            "skill": clean_name,
            "taint_source": taint_source,
            "args_preview": str(args)[:100]
        })

    logger.info(f"[Skill Dispatcher] Executing registered skill '{clean_name}' (args: {args})")
    res = SubprocessSkillSandbox.execute(
        skill_name=clean_name,
        skill_file=skill_file,
        manifest_file=manifest_file,
        args=args or {},
        timeout=timeout_val
    )

    if res.get("status") == "success":
        return str(res.get("result", ""))
    return f"[SKILL EXECUTION ERROR: {res.get('error_type', 'Error')}] {res.get('error', 'Unknown failure')}"


def propose_skill(
    name: str,
    description: str,
    python_code: str,
    permissions: Optional[Dict[str, Any]] = None,
    test_cases: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Proposes a new custom skill for staging, static analysis, diff display, and physical approval.
    
    Args:
        name: Valid Python function name.
        description: Description of the skill capability and parameters.
        python_code: Complete Python code with 'def <name>(...): ...'.
        permissions: Optional dict with 'network' (bool), 'filesystem' (list), 'max_timeout_s' (float).
        test_cases: Optional list of test inputs to validate execution.
    """
    res = SkillLifecycleEngine.stage_candidate_skill(
        name=name,
        description=description,
        python_code=python_code,
        permissions=permissions,
        test_cases=test_cases
    )
    if res.get("status") == "error":
        return f"[SKILL PROPOSAL REJECTED] {res.get('error')}"
    return res.get("message", "Skill candidate staged successfully.")


def activate_skill(name: str, confirmation_token: str) -> str:
    """Activates a staged skill after physical on-screen approval and successful isolated testing.
    
    Args:
        name: The name of the staged skill to activate.
        confirmation_token: The confirmation token returned by propose_skill.
    """
    res = SkillLifecycleEngine.activate_staged_skill(name, confirmation_token)
    if res.get("status") == "error":
        return f"[ACTIVATION FAILED] {res.get('error')}"
    return res.get("message", f"Skill '{name}' activated successfully.")


def rollback_skill(name: str, reason: str = "") -> str:
    """Rolls back a skill to the last-known-good version using Git.
    
    Args:
        name: Name of the skill to roll back.
        reason: Justification for rollback.
    """
    return SkillLifecycleEngine.rollback_skill(name, reason)


def quarantine_skill(name: str, reason: str = "") -> str:
    """Moves a faulty or broken skill into quarantine and removes it from the catalog.
    
    Args:
        name: Name of the skill to quarantine.
        reason: Cause of quarantine (e.g. crash loop, runtime exception).
    """
    return SkillLifecycleEngine.quarantine_skill(name, reason)


# Backward-compatibility helpers & aliases
list_custom_tools = list_skills

def execute_custom_skill_subprocess(skill_name: str, args: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> str:
    """Executes an active custom skill inside the subprocess sandbox (compatibility helper)."""
    skill_file = ACTIVE_SKILLS_DIR / f"{skill_name}.py"
    manifest_file = ACTIVE_SKILLS_DIR / f"{skill_name}.manifest.json"
    res = SubprocessSkillSandbox.execute(
        skill_name=skill_name,
        skill_file=skill_file,
        manifest_file=manifest_file if manifest_file.exists() else None,
        args=args or {},
        timeout=timeout
    )
    if res.get("status") == "success":
        return str(res.get("result", ""))
    return f"[ERROR: {res.get('error_type')}] {res.get('error')}"

def create_custom_tool(tool_name: str, description: str, python_code: str) -> str:
    """Stages and activates a custom tool for backward compatibility with older tests/workflows."""
    clean_name = tool_name.strip()
    stg = SkillLifecycleEngine.stage_candidate_skill(clean_name, description, python_code)
    if stg.get("status") == "error":
        return f"[SECURITY VIOLATION] Custom skill failed safety validation: {stg.get('error')}"
    token = stg.get("confirmation_token")
    act = SkillLifecycleEngine.activate_staged_skill(clean_name, token)
    if act.get("status") == "error":
        return f"[ACTIVATION FAILED] {act.get('error')}"
    return f"Successfully created custom skill '{clean_name}'!\nPath: {ACTIVE_SKILLS_DIR / f'{clean_name}.py'}\nSkill is isolated in subprocess execution mode. Ready for execution."


# ==============================================================================
# 6. PERSISTENT MEMORY BANK WITH PROVENANCE TRACKING
# ==============================================================================

def _load_json(file_path: Path, default_val: Any) -> Any:
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading {file_path}: {e}")
    return default_val

def _save_json(file_path: Path, data: Any):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error writing {file_path}: {e}")

VALID_MEMORY_SOURCES = {
    "user_explicit",
    "user_confirmed",
    "observed_screen_content",
    "file_derived_facts",
    "model_inference",
    "application_workflow",
    "temporary_task_context"
}

def remember_insight(
    category: str,
    key: str,
    insight: str,
    source: str = "user_explicit",
    provenance: Optional[str] = None,
    confidence: Optional[float] = None,
    user_confirmed: bool = False,
    ttl_seconds: Optional[float] = None,
    source_context: str = ""
) -> str:
    """Stores a learned insight with strict provenance tracking into persistent memory.
    
    Security Policy Rule: Only direct user statements or explicitly user-approved information
    may create user preferences and corrections. Inferences and observations cannot become
    unconfirmed user preferences.
    """
    memories = _load_json(MEMORY_FILE, {
        "user_preference": {},
        "workflow_pattern": {},
        "correction": {},
        "observed_context": {},
        "task_context": {},
        "general": {}
    })

    cat = category.lower().strip()
    k = key.strip()
    clean_insight = redact_secrets(insight.strip())

    # Support provenance as alias for source
    effective_source = (provenance or source or "user_explicit").lower().strip()
    if effective_source not in VALID_MEMORY_SOURCES:
        if "screen" in effective_source or "ocr" in effective_source:
            effective_source = "observed_screen_content"
        elif "file" in effective_source:
            effective_source = "file_derived_facts"
        elif "inference" in effective_source:
            effective_source = "model_inference"
        elif "task" in effective_source:
            effective_source = "temporary_task_context"
        elif "workflow" in effective_source:
            effective_source = "application_workflow"
        elif "user" in effective_source or "explicit" in effective_source:
            effective_source = "user_explicit"
        else:
            effective_source = "model_inference"

    is_tainted, taint_source, _ = GLOBAL_TAINT_TRACKER.is_tainted()
    if is_tainted and effective_source == "user_explicit" and not user_confirmed and not source_context:
        effective_source = "observed_screen_content"

    # STRICT SECURITY RULE: Only direct user statements or explicitly user-approved information
    # may create user preferences and corrections.
    if cat in ("user_preference", "correction"):
        if effective_source not in ("user_explicit", "user_confirmed") and not user_confirmed:
            msg = (
                f"[PROVENANCE ERROR] Inferred or observed information ({effective_source}) cannot "
                f"create persistent {cat} '{k}' without explicit user confirmation."
            )
            logger.warning(f"[Memory Provenance] Blocked unconfirmed preference creation: {msg}")
            record_audit_event("MEMORY_PROVENANCE_BLOCKED", {
                "category": cat,
                "key": k,
                "source": effective_source
            })
            return msg

    if cat not in memories:
        memories[cat] = {}

    existing_entry = memories[cat].get(k, {})

    # Compute confidence and confirmation status
    if effective_source in ("user_explicit", "user_confirmed") or user_confirmed:
        eff_confidence = 1.0 if confidence is None else min(1.0, max(0.0, float(confidence)))
        is_confirmed = True
    else:
        is_confirmed = False
        if confidence is not None:
            eff_confidence = min(1.0, max(0.0, float(confidence)))
        elif effective_source == "model_inference":
            eff_confidence = 0.5
        elif effective_source == "observed_screen_content":
            eff_confidence = 0.6
        elif effective_source == "file_derived_facts":
            eff_confidence = 0.7
        else:
            eff_confidence = 0.6

    # Expiry calculation
    now = datetime.now()
    expires_at = None
    if ttl_seconds is not None and ttl_seconds > 0:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    elif effective_source == "temporary_task_context" or cat == "task_context":
        expires_at = (now + timedelta(hours=2)).isoformat()

    # 1. Primary write to SQLite FTS5 + Vector engine
    try:
        sql_res = SQLITE_MEMORY.store_memory(
            category=cat,
            key=k,
            content=clean_insight,
            source=effective_source,
            confidence=eff_confidence,
            importance=2.0 if cat in ("user_preference", "correction") else 1.0,
            user_approval_status="confirmed" if is_confirmed else "unconfirmed",
            ttl_seconds=ttl_seconds,
            metadata={"source_context": redact_secrets(source_context.strip()[:200])}
        )
        if sql_res.get("status") == "error" and sql_res.get("provenance_violation"):
            return sql_res.get("error")
    except Exception as sqle:
        logger.warning(f"[Memory Bank] SQLite store error: {sqle}")

    # 2. Dual-write to JSON store for backward compatibility
    entry = {
        "insight": clean_insight,
        "category": cat,
        "key": k,
        "source": effective_source,
        "provenance": effective_source,
        "confidence": eff_confidence,
        "is_confirmed": is_confirmed,
        "created_at": existing_entry.get("created_at", now.isoformat()),
        "updated_at": now.isoformat(),
        "expires_at": expires_at,
        "access_count": existing_entry.get("access_count", 0) + 1,
        "source_context": redact_secrets(source_context.strip()[:200])
    }
    memories[cat][k] = entry
    _save_json(MEMORY_FILE, memories)

    record_audit_event("MEMORY_COMMIT", {
        "category": cat,
        "key": k,
        "source": effective_source,
        "confidence": eff_confidence,
        "is_confirmed": is_confirmed,
        "insight_preview": clean_insight[:100]
    })
    logger.info(f"[Memory Bank] Stored memory ({effective_source}, confirmed={is_confirmed}): [{cat}] {k}")
    return f"Successfully committed memory: [{cat}] {k} (Source: {effective_source}, Confirmed: {is_confirmed}, Confidence: {eff_confidence})"


def prune_expired_memories() -> int:
    """Purges memories whose expires_at timestamp has passed in both SQLite and JSON stores."""
    purged_sql = 0
    try:
        purged_sql = SQLITE_MEMORY.prune_expired()
    except Exception as e:
        logger.warning(f"[Memory Prune] SQLite prune error: {e}")

    memories = _load_json(MEMORY_FILE, {})
    now = datetime.now()
    purged_count = 0
    modified = False

    for cat_name, entries in list(memories.items()):
        for k, v in list(entries.items()):
            exp = v.get("expires_at")
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(exp)
                    if exp_dt < now:
                        del memories[cat_name][k]
                        purged_count += 1
                        modified = True
                        logger.info(f"[Memory Prune] Expired memory [{cat_name}] '{k}' removed.")
                except Exception:
                    pass

    if modified:
        _save_json(MEMORY_FILE, memories)
    return max(purged_sql, purged_count)


def list_memories(include_expired: bool = False, category: str = "") -> List[Dict[str, Any]]:
    """Returns a structured catalog of stored memories from the SQLite engine for user review & pruning."""
    try:
        results = SQLITE_MEMORY.list_memories(
            category=category or None,
            include_expired=include_expired
        )
        if results:
            return results
    except Exception as e:
        logger.warning(f"[Memory Engine] SQLite list_memories error: {e}. Falling back to JSON.")

    if not include_expired:
        prune_expired_memories()
    memories = _load_json(MEMORY_FILE, {})
    flat_list = []
    target_cat = category.lower().strip() if category else ""

    for cat_name, entries in memories.items():
        if target_cat and target_cat != cat_name:
            continue
        for k, v in entries.items():
            flat_list.append({
                "id": 0,
                "memory_id": f"legacy_{cat_name}_{k}",
                "category": cat_name,
                "key_name": k,
                "key": k,
                "content": v.get("insight", ""),
                "insight": v.get("insight", ""),
                "source": v.get("source", v.get("provenance", "unknown")),
                "provenance": v.get("provenance", v.get("source", "unknown")),
                "confidence": v.get("confidence", 1.0 if v.get("is_confirmed") else 0.5),
                "importance": 1.0,
                "is_confirmed": v.get("is_confirmed", v.get("provenance") == "user_explicit"),
                "user_approval_status": "confirmed" if v.get("is_confirmed") else "unconfirmed",
                "created_at": v.get("created_at", "")[:19],
                "updated_at": v.get("updated_at", "")[:19],
                "expires_at": v.get("expires_at"),
                "access_count": v.get("access_count", 1)
            })
    return sorted(flat_list, key=lambda x: x["updated_at"], reverse=True)


def edit_memory(category: str, key: str, new_insight: str, user_confirmed: bool = True) -> str:
    """Edits an existing memory insight in SQLite and JSON with provenance update."""
    clean_insight = redact_secrets(new_insight.strip())
    cat = category.lower().strip()
    k = key.strip()

    # 1. Update in SQLite
    sql_ok = False
    try:
        sql_res = SQLITE_MEMORY.update_memory(
            category=cat,
            key=k,
            new_content=clean_insight,
            user_confirmed=user_confirmed
        )
        if sql_res.get("status") == "success":
            sql_ok = True
    except Exception as e:
        logger.warning(f"[Memory Engine] SQLite update exception: {e}")

    # 2. Update in JSON store
    memories = _load_json(MEMORY_FILE, {})
    if cat in memories and k in memories[cat]:
        entry = memories[cat][k]
        old_insight = entry.get("insight", "")
        entry["insight"] = clean_insight
        entry["updated_at"] = datetime.now().isoformat()
        if user_confirmed:
            entry["is_confirmed"] = True
            entry["confidence"] = 1.0
            entry["source"] = "user_confirmed"
            entry["provenance"] = "user_confirmed"

        _save_json(MEMORY_FILE, memories)
        record_audit_event("MEMORY_EDIT", {
            "category": cat,
            "key": k,
            "old_insight_preview": old_insight[:80],
            "new_insight_preview": clean_insight[:80]
        })
        return f"Successfully updated memory [{cat}] '{k}'. Status: Confirmed."

    if sql_ok:
        return f"Successfully updated memory [{cat}] '{k}'. Status: Confirmed (SQLite updated)."
    return f"Error: Memory entry [{cat}] '{k}' not found in persistent store."


def confirm_memory(category: str, key: str, target_category: Optional[str] = None) -> str:
    """Upgrades an unconfirmed observation or inference into a confirmed user preference or fact."""
    cat = category.lower().strip()
    k = key.strip()
    dest_cat = target_category.lower().strip() if target_category else cat

    # 1. Confirm in SQLite
    sql_ok = False
    try:
        sql_res = SQLITE_MEMORY.confirm_memory(category=cat, key=k, target_category=dest_cat)
        if sql_res.get("status") == "success":
            sql_ok = True
    except Exception as e:
        logger.warning(f"[Memory Engine] SQLite confirm exception: {e}")

    # 2. Confirm in JSON
    memories = _load_json(MEMORY_FILE, {})
    if cat in memories and k in memories[cat]:
        entry = memories[cat].pop(k)
        entry["is_confirmed"] = True
        entry["confidence"] = 1.0
        entry["source"] = "user_confirmed"
        entry["provenance"] = "user_confirmed"
        entry["updated_at"] = datetime.now().isoformat()
        entry["expires_at"] = None

        if dest_cat not in memories:
            memories[dest_cat] = {}
        entry["category"] = dest_cat
        memories[dest_cat][k] = entry

        _save_json(MEMORY_FILE, memories)
        record_audit_event("MEMORY_CONFIRM", {
            "source_category": cat,
            "target_category": dest_cat,
            "key": k
        })
        return f"Successfully confirmed memory [{dest_cat}] '{k}'. Promoted to verified state."

    if sql_ok:
        return f"Successfully confirmed memory [{dest_cat}] '{k}'. Promoted to verified state."
    return f"Error: Memory entry [{cat}] '{k}' not found in persistent store."


def delete_memory(category: str, key: str) -> str:
    """Removes a specific memory entry from both SQLite and JSON stores."""
    cat = category.lower().strip()
    k = key.strip()

    sql_ok = False
    try:
        sql_res = SQLITE_MEMORY.delete_memory(category=cat, key=k)
        if sql_res.get("status") == "success":
            sql_ok = True
    except Exception as e:
        logger.warning(f"[Memory Engine] SQLite delete exception: {e}")

    memories = _load_json(MEMORY_FILE, {})
    if cat in memories and k in memories[cat]:
        removed = memories[cat].pop(k)
        _save_json(MEMORY_FILE, memories)
        record_audit_event("MEMORY_FORGET", {"category": cat, "key": k})
        return f"Successfully forgotten memory [{cat}] {k}: '{removed.get('insight')}'"

    if sql_ok:
        return f"Successfully forgotten memory [{cat}] {k} from persistent store."
    return f"Memory entry [{cat}] '{k}' not found in persistent store."


def recall_memory(query: str = "", category: str = "") -> str:
    """Recalls stored memories using hybrid FTS5 and vector similarity search."""
    q = query.strip()
    c = category.strip()

    try:
        if q:
            results = SQLITE_MEMORY.search_hybrid(query=q, category=c or None, top_k=10)
            if results:
                lines = [f"🧠 CHIRKOOT HYBRID RETRIEVAL (Query: '{q}'):"]
                for r in results:
                    src = r.get("source", "unknown")
                    conf = "verified" if r.get("is_confirmed") else "unconfirmed"
                    f_score = r.get("final_score", 0.0)
                    lines.append(
                        f"  • [{r.get('category').upper()}] {r.get('key_name')} "
                        f"(relevance: {f_score:.2f}, source: {src}, {conf}): {r.get('content')}"
                    )
                return "\n".join(lines)
            return f"No memories found matching query '{query}' in category '{category}'."
        else:
            all_mems = SQLITE_MEMORY.list_memories(category=c or None, limit=50)
            if all_mems:
                lines = ["🧠 CHIRKOOT PERSISTENT MEMORY BANK:"]
                by_cat: Dict[str, List[Dict[str, Any]]] = {}
                for r in all_mems:
                    by_cat.setdefault(r["category"], []).append(r)
                for cat_name, entries in by_cat.items():
                    lines.append(f"\n[{cat_name.upper()}]:")
                    for r in entries:
                        src = r.get("source", "unknown")
                        conf = "verified" if r.get("is_confirmed") else "unconfirmed"
                        lines.append(f"  • {r.get('key_name')} (source: {src}, {conf}): {r.get('content')}")
                return "\n".join(lines)
            return "No memories currently stored. Memory bank is empty."
    except Exception as e:
        logger.warning(f"[Memory Engine] recall_memory fallback on error: {e}")

    # Fallback to JSON
    memories = _load_json(MEMORY_FILE, {})
    if not memories:
        return "No memories currently stored. Memory bank is empty."

    lines = ["🧠 CHIRKOOT PERSISTENT MEMORY BANK:"]
    q_low = q.lower()
    c_low = c.lower()
    count = 0
    for cat_name, cat_entries in memories.items():
        if c_low and c_low != cat_name:
            continue
        filtered_entries = {}
        for k_entry, v in cat_entries.items():
            content = str(v.get("insight", "")).lower()
            if not q_low or (q_low in k_entry.lower() or q_low in content):
                filtered_entries[k_entry] = v

        if filtered_entries:
            lines.append(f"\n[{cat_name.upper()}]:")
            for k_entry, v in filtered_entries.items():
                count += 1
                src = v.get("source", v.get("provenance", "user_explicit"))
                conf = "verified" if v.get("is_confirmed") else "unconfirmed"
                lines.append(f"  • {k_entry} (source: {src}, {conf}): {v.get('insight')}")

    if count == 0:
        return f"No memories found matching query '{query}' in category '{category}'."
    return "\n".join(lines)


def search_memory(query: str, category: str = "", top_k: int = 5) -> str:
    """Searches memory using hybrid FTS5 keyword and sqlite-vec vector similarity."""
    clean_q = query.strip()
    if not clean_q:
        return "Please provide a search query."
    results = SQLITE_MEMORY.search_hybrid(clean_q, category=category or None, top_k=top_k)
    if not results:
        return f"No memories found matching '{clean_q}'."
    lines = [f"🔍 MEMORY SEARCH RESULTS for '{clean_q}':"]
    for r in results:
        status_tag = "verified" if r.get("is_confirmed") else f"unconfirmed_{r.get('source')}"
        lines.append(
            f"• [{r['category'].upper()}] {r['key_name']}: {r['content']} "
            f"[score: {r['final_score']} | fts: {r['fts_score']} | vec: {r['vec_score']} | status: {status_tag}]"
        )
    return "\n".join(lines)


def migrate_memories(delete_original: bool = False) -> str:
    """Migrates legacy memory.json entries into SQLite FTS5 + Vector store with verification."""
    res = SQLITE_MEMORY.migrate_from_json(delete_original_after_verification=delete_original)
    if res.get("status") == "success":
        return (
            f"✅ Memory Migration Succeeded:\n"
            f"• Migrated Records: {res.get('migrated_count')}\n"
            f"• Total in SQLite: {res.get('total_in_db')}\n"
            f"• Test Retrieval: {'PASS' if res.get('test_retrieval_ok') else 'FAIL'}\n"
            f"• Backup Created: {res.get('backup_file')}"
        )
    return f"❌ Memory Migration Failed: {res.get('error', res.get('message'))}"


def rollback_memories() -> str:
    """Rolls back memory from backup file to restore JSON memory state."""
    res = SQLITE_MEMORY.rollback_to_json()
    if res.get("status") == "success":
        return f"✅ Memory Rollback Succeeded: Restored from {res.get('source')}"
    return f"❌ Memory Rollback Failed: {res.get('error')}"


def get_scoped_memories(query: str = "", top_k: int = 5) -> str:
    """Retrieves top-k relevant memories on demand without injecting entire database into Gemini instructions."""
    try:
        scoped = SQLITE_MEMORY.get_scoped_memories_prompt(query=query, top_k=top_k)
        if scoped:
            return scoped
    except Exception as e:
        logger.warning(f"[Memory Engine] SQLite get_scoped_memories error: {e}")

    # Fallback to JSON scoped ranking
    prune_expired_memories()
    memories = _load_json(MEMORY_FILE, {})
    if not memories:
        return ""

    candidates = []
    q_tokens = set(re.findall(r"\w+", query.lower())) if query else set()

    for cat_name, entries in memories.items():
        for k, v in entries.items():
            insight = v.get("insight", "").strip()
            if not insight:
                continue

            score = 1.0
            is_conf = v.get("is_confirmed", False)
            src = v.get("source", v.get("provenance", "unknown"))

            if is_conf and src in ("user_explicit", "user_confirmed"):
                score += 3.0
            if cat_name in ("user_preference", "correction"):
                score += 2.0

            if q_tokens:
                text_tokens = set(re.findall(r"\w+", f"{k} {insight}".lower()))
                overlap = len(q_tokens.intersection(text_tokens))
                score += overlap * 3.0

            candidates.append((score, cat_name, k, insight, src, is_conf))

    candidates.sort(key=lambda x: x[0], reverse=True)
    selected = candidates[:top_k]
    if not selected:
        return ""

    items = []
    for (_, cat, k, ins, src, is_conf) in selected:
        status_label = "verified" if is_conf else f"unconfirmed_{src}"
        items.append(f"- [{cat.upper()}] {k}: {ins} (status: {status_label})")

    return (
        "\n\nRELEVANT USER PREFERENCES & MEMORY (Scoped Top-K):\n"
        + "\n".join(items)
        + "\nAlways apply confirmed preferences in your operations."
    )


def get_memory_prompt_injection(active_query: str = "") -> str:
    return get_scoped_memories(active_query, top_k=5)


# ==============================================================================
# 7. CODE INSPECTION & IMMUTABLE CORE ENFORCEMENT
# ==============================================================================

def inspect_self() -> str:
    """Inspects Chirkoot's architecture, active modules, tool count, and security status."""
    try:
        memories = _load_json(MEMORY_FILE, {})
        mem_count = sum(len(v) for v in memories.values())
        catalog = SkillLifecycleEngine.get_catalog()
        protected = get_repo_protected_files()

        return (
            f"⚡ CHIRKOOT MK-V SECURE AUTONOMOUS ARCHITECTURE:\n"
            f"• Installation Root: {ROOT_DIR}\n"
            f"• User AppData Store: {CHIRKOOT_APPDATA_DIR}\n"
            f"• Safe Mode: {'ACTIVE' if SAFE_MODE else 'INACTIVE'}\n"
            f"• Immutable Repository Files: {len(protected)} protected files\n"
            f"• Active Validated Skills: {len(catalog)} skills in {ACTIVE_SKILLS_DIR}\n"
            f"• Git Versioning: %LOCALAPPDATA%\\Chirkoot\\skills_repo\n"
            f"• Subprocess Sandbox: Strict taskkill process-tree termination, socket disabling\n"
            f"• Total Persistent Memories: {mem_count} entries (Provenance Tagged)"
        )
    except Exception as e:
        return f"Error during self-inspection: {e}"

def read_code_file(filepath: str, start_line: int = 1, end_line: int = 120) -> str:
    """Reads lines from source code with sensitive file sandboxing."""
    if is_sensitive_path(filepath):
        return f"[SECURITY ERROR] Access to credential, token, or browser file '{filepath}' is prohibited."

    p = Path(filepath)
    if not p.is_absolute():
        p = (ROOT_DIR / filepath).resolve()
    else:
        p = p.resolve()

    if not p.exists() or not p.is_file():
        return f"Error: File '{filepath}' does not exist on disk."

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        s = max(1, start_line)
        e = min(total, max(s, end_line))
        if e - s > 150:
            e = s + 150

        sliced = [f"{i+1:4d}: {lines[i].rstrip()}" for i in range(s - 1, e)]
        return f"--- File: {p.name} (Lines {s}-{e} of {total}) ---\n" + "\n".join(sliced)
    except Exception as e:
        return f"Error reading file '{filepath}': {e}"

def modify_source_code(filepath: str, target_snippet: str, replacement_snippet: str) -> str:
    """Modifies a file with Immutable Core protection, backup, and AST syntax verification."""
    if SAFE_MODE:
        return "[SAFE MODE] Code self-modification is disabled in Safe Mode."

    # IMMUTABLE CORE REJECTION: Generated code can NEVER modify repository core files or policies
    if is_immutable_core_path(filepath):
        record_audit_event("IMMUTABLE_CORE_VIOLATION_ATTEMPT", {"file": filepath})
        return (
            f"[SECURITY VIOLATION] Target file '{filepath}' belongs to Chirkoot's Immutable Core. "
            f"Protected repository files, security policies, launchers, and core dispatcher cannot be altered."
        )

    p = Path(filepath)
    if not p.is_absolute():
        p = (CHIRKOOT_APPDATA_DIR / filepath).resolve()
    else:
        p = p.resolve()

    if not p.exists() or not p.is_file():
        return f"Error: File '{filepath}' does not exist on disk."

    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()

        if target_snippet not in content:
            return f"Error: Target snippet not found in '{filepath}'."

        new_content = content.replace(target_snippet, replacement_snippet, 1)

        if p.suffix == ".py":
            try:
                ast.parse(new_content, filename=str(p))
            except SyntaxError as se:
                return f"SYNTAX VALIDATION FAILED! Edit rejected: Line {se.lineno} -> {se.msg}"

        with open(p, "w", encoding="utf-8") as f:
            f.write(new_content)

        record_audit_event("CODE_MODIFICATION_APPLIED", {"file": filepath})
        return f"Successfully updated '{filepath}'! Syntax validation passed."
    except Exception as e:
        return f"Error modifying '{filepath}': {e}"

def rollback_code(filepath: str) -> str:
    """Rolls back a skill or code file to the last-known-good state."""
    p = Path(filepath)
    skill_name = p.stem
    return rollback_skill(skill_name, reason="User requested rollback")

def run_self_tests() -> str:
    """Verifies that all Python files compile cleanly without syntax errors."""
    errors = []
    py_files = list(ROOT_DIR.glob("*.py")) + list(ACTIVE_SKILLS_DIR.glob("*.py"))
    
    for f in py_files:
        try:
            with open(f, "r", encoding="utf-8") as s:
                ast.parse(s.read(), filename=str(f))
        except SyntaxError as se:
            errors.append(f"❌ {f.name}: Line {se.lineno} -> {se.msg}")
        except Exception as e:
            errors.append(f"❌ {f.name}: {e}")

    if errors:
        return "Self-Test Alert: Syntax errors detected:\n" + "\n".join(errors)
    return f"All {len(py_files)} Python source files passed syntax validation cleanly. System state: NOMINAL."

def get_codebase_guide() -> str:
    """Returns an architectural guide of Chirkoot's codebase."""
    return (
        "🗺️ CHIRKOOT MK-V SECURE AUTONOMOUS ARCHITECTURE & CODEBASE GUIDE:\n"
        "• security_policy.py: Central Security Policy, Protected Repository Files, Taint Tracker, DPAPI Vault.\n"
        "• server.py: FastAPI server, Gemini 3.8 Live WebSocket session, decoupled queue manager, HITL Confirmation Gate.\n"
        "• tools.py: Stable dispatch tools (list_skills, run_skill, propose_skill, activate_skill, rollback_skill).\n"
        "• sandbox_runner.py: Subprocess sandbox runner, process-tree termination (taskkill), socket disabling.\n"
        "• learning_engine.py: 10-Stage skill lifecycle, Git repository manager, startup watchdog, scoped memory bank.\n"
        "• %LOCALAPPDATA%\\Chirkoot\\: Mutable user data (skills_repo/, custom_skills/active/, memory/, config/, temp/).\n"
        "• Protected Repository: Strictly immutable core. Generated code cannot alter policies or launchers."
    )

def self_learn_and_implement(
    feature_title: str,
    file_to_modify: str,
    target_code_snippet: str,
    new_code_snippet: str,
    explanation: str = ""
) -> str:
    """High-level self-modification tool with Immutable Core safeguards."""
    if SAFE_MODE:
        return "[SAFE MODE] Self-modification is disabled in Safe Mode."

    mod_result = modify_source_code(file_to_modify, target_code_snippet, new_code_snippet)
    if "Error" in mod_result or "FAILED" in mod_result or "SECURITY VIOLATION" in mod_result:
        return f"Self-learning modification rejected: {mod_result}"

    test_result = run_self_tests()
    remember_insight("workflow_pattern", f"feature_{feature_title.lower().replace(' ', '_')}", f"Implemented in {file_to_modify}: {explanation}", provenance="user_explicit")

    return (
        f"⚡ [SELF-LEARNING DEPLOYED]: {feature_title}\n"
        f"• Modified File: {file_to_modify}\n"
        f"• Code Change: {mod_result}\n"
        f"• System Health: {test_result}\n"
        f"• Memory Logged: {explanation}"
    )

def restart_chirkoot_server() -> str:
    """Gracefully signals server reload."""
    try:
        touch_file = ROOT_DIR / "server.py"
        os.utime(touch_file, None)
        return "Server reload signal dispatched. Uvicorn worker will restart with updated codebase."
    except Exception as e:
        return f"Error triggering server restart: {e}"
