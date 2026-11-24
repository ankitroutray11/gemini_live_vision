"""
CLI Utility to securely set or verify the Gemini API key in the Windows DPAPI vault.
Usage:
    python set_api_key.py
    python set_api_key.py AIzaSy...
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

import security_policy as sp

def main():
    if len(sys.argv) > 1:
        key = sys.argv[1].strip()
    else:
        print("\n" + "=" * 65)
        print(" CHIRKOOT MK-V: SECURE GEMINI API KEY CONFIGURATION (DPAPI)")
        print("=" * 65)
        current = sp.get_gemini_api_key()
        if current:
            masked = f"{current[:4]}...{current[-4:]}" if len(current) > 8 else "***"
            print(f"Current Configured Key: {masked}")
        else:
            print("Current Configured Key: [NONE CONFIGURED]")
        print("\nEnter your Gemini API key (from https://aistudio.google.com/app/apikey):")
        try:
            key = input("API Key: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.")
            return 1

    if not key:
        print("[ERROR] No API key provided. Operation cancelled.")
        return 1

    if key in ("TEST_GEMINI_KEY_9999", "your_api_key_here"):
        print("[ERROR] Cannot use test placeholder string as API key.")
        return 1

    ok = sp.store_secret_dpapi("GEMINI_API_KEY", key)
    if ok:
        print("\n[SUCCESS] Gemini API key encrypted and saved to Windows DPAPI vault.")
        print(f"Stored securely in: {sp.VAULT_FILE}")
        return 0
    else:
        print("\n[ERROR] Failed to save API key to Windows DPAPI vault.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
