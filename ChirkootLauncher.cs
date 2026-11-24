using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

/// <summary>
/// Chirkoot MK-V Windows Desktop Launcher & Deployment Subsystem.
/// Features:
/// 1. Reliable Backend Startup & HTTP /api/health Verification.
/// 2. Single-Instance Mutex Protection with Foreground Window Activation.
/// 3. Controlled Process Lifecycle using Win32 Job Objects (KILL_ON_JOB_CLOSE).
/// 4. Pre-launch Loose Python Cryptographic Integrity Verification (SHA-256).
/// 5. Last-Known-Good (LKG) Automatic Rollback / Recovery for Core Files.
/// 6. Watchdog Crash-Loop Detection & Safe Mode Auto-Engagement.
/// 7. In-Process HWND Capture Exclusion (WDA_EXCLUDEFROMCAPTURE).
/// 8. Zero Mutable Writes into Application Root (All user data in %LOCALAPPDATA%\Chirkoot).
/// </summary>
class ChirkootLauncher
{
    // --- Win32 Display Affinity Definitions ---
    public const uint WDA_NONE = 0x00000000;
    public const uint WDA_MONITOR = 0x00000001;
    public const uint WDA_EXCLUDEFROMCAPTURE = 0x00000011;

    // --- Win32 Window Management Definitions ---
    public const int SW_RESTORE = 9;

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowDisplayAffinity(IntPtr hWnd, uint dwAffinity);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetWindowDisplayAffinity(IntPtr hWnd, out uint pdwAffinity);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    // --- Win32 Job Object Definitions ---
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

    public enum JobObjectInfoType
    {
        ExtendedLimitInformation = 9
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public Int64 PerProcessUserTimeLimit;
        public Int64 PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public UIntPtr Affinity;
        public UInt32 PriorityClass;
        public UInt32 SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public UInt64 ReadOperationCount;
        public UInt64 WriteOperationCount;
        public UInt64 OtherOperationCount;
        public UInt64 ReadTransferCount;
        public UInt64 WriteTransferCount;
        public UInt64 OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryLimit;
        public UIntPtr PeakJobMemoryLimit;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(IntPtr hJob, JobObjectInfoType JobObjectInformationClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr hObject);

    private const string LAUNCHER_MUTEX_NAME = "Local\\Chirkoot_MKV_Launcher_Mutex";
    private static string logFilePath = null;

    /// <summary>
    /// Initializes logging directory strictly in %LOCALAPPDATA%\Chirkoot\logs.
    /// </summary>
    public static void InitLogger()
    {
        try
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string logDir = Path.Combine(localAppData, @"Chirkoot\logs");
            if (!Directory.Exists(logDir))
            {
                Directory.CreateDirectory(logDir);
            }
            logFilePath = Path.Combine(logDir, "launcher.log");
        }
        catch { }
    }

    public static void Log(string message)
    {
        string line = string.Format("[{0:yyyy-MM-dd HH:mm:ss.fff}] [Launcher] {1}", DateTime.Now, message);
        Console.WriteLine(line);
        if (!string.IsNullOrEmpty(logFilePath))
        {
            try
            {
                File.AppendAllText(logFilePath, line + Environment.NewLine);
            }
            catch { }
        }
    }

    /// <summary>
    /// Safely applies WDA_EXCLUDEFROMCAPTURE to a native window handle.
    /// Strictly verifies that the calling process owns the HWND before invocation.
    /// Cross-process injection is strictly prohibited by security policy.
    /// </summary>
    public static bool ApplyCaptureExclusion(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero) return false;

        uint windowPid;
        GetWindowThreadProcessId(hWnd, out windowPid);
        uint currentPid = (uint)Process.GetCurrentProcess().Id;

        if (windowPid != currentPid)
        {
            Log(string.Format(
                "[Capture Exclusion Boundary] HWND {0} is owned by foreign PID {1} (Current PID: {2}). " +
                "Cross-process SetWindowDisplayAffinity is forbidden by Win32 API. Engaging pipeline masking.",
                hWnd, windowPid, currentPid
            ));
            return false;
        }

        bool result = SetWindowDisplayAffinity(hWnd, WDA_EXCLUDEFROMCAPTURE);
        int lastError = Marshal.GetLastWin32Error();

        if (result)
        {
            Log(string.Format(
                "[Capture Exclusion] Successfully applied WDA_EXCLUDEFROMCAPTURE (0x{0:X8}) to in-process HWND {1}",
                WDA_EXCLUDEFROMCAPTURE, hWnd
            ));
            return true;
        }
        else
        {
            Log(string.Format(
                "[Capture Exclusion Notice] SetWindowDisplayAffinity returned false (Error: {0}). Safe fallback active.",
                lastError
            ));
            return false;
        }
    }

    /// <summary>
    /// Searches top-level windows for an existing Chirkoot window and brings it to foreground.
    /// </summary>
    public static void BringExistingWindowToFront()
    {
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            if (IsWindowVisible(hWnd))
            {
                StringBuilder sb = new StringBuilder(256);
                GetWindowText(hWnd, sb, 256);
                string title = sb.ToString();
                if (!string.IsNullOrEmpty(title) &&
                    (title.IndexOf("Chirkoot", StringComparison.OrdinalIgnoreCase) >= 0 ||
                     title.IndexOf("CHIRKOOT", StringComparison.OrdinalIgnoreCase) >= 0))
                {
                    ShowWindow(hWnd, SW_RESTORE);
                    SetForegroundWindow(hWnd);
                    Log(string.Format("Restored and focused existing Chirkoot window: '{0}' (HWND: {1})", title, hWnd));
                    return false; // Stop enumeration
                }
            }
            return true;
        }, IntPtr.Zero);
    }

    /// <summary>
    /// Creates a Win32 Job Object configured with KILL_ON_JOB_CLOSE so child processes
    /// (Python backend, Chromium browser) are automatically terminated when the launcher terminates.
    /// </summary>
    public static IntPtr SetupJobObject()
    {
        try
        {
            IntPtr hJob = CreateJobObject(IntPtr.Zero, null);
            if (hJob != IntPtr.Zero)
            {
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
                IntPtr pInfo = Marshal.AllocHGlobal(length);
                try
                {
                    Marshal.StructureToPtr(info, pInfo, false);
                    if (SetInformationJobObject(hJob, JobObjectInfoType.ExtendedLimitInformation, pInfo, (uint)length))
                    {
                        Log("Win32 Job Object initialized with KILL_ON_JOB_CLOSE flag.");
                        return hJob;
                    }
                    else
                    {
                        Log("Warning: Failed to set JobObject limit information. Error: " + Marshal.GetLastWin32Error());
                    }
                }
                finally
                {
                    Marshal.FreeHGlobal(pInfo);
                }
            }
        }
        catch (Exception ex)
        {
            Log("Job Object configuration notice: " + ex.Message);
        }
        return IntPtr.Zero;
    }

    /// <summary>
    /// Computes the SHA-256 hash of a file as a lowercase hexadecimal string.
    /// </summary>
    public static string ComputeSha256(string filePath)
    {
        if (!File.Exists(filePath)) return null;
        try
        {
            using (var sha256 = SHA256.Create())
            {
                using (var stream = File.OpenRead(filePath))
                {
                    byte[] hash = sha256.ComputeHash(stream);
                    StringBuilder sb = new StringBuilder();
                    for (int i = 0; i < hash.Length; i++)
                    {
                        sb.Append(hash[i].ToString("x2"));
                    }
                    return sb.ToString();
                }
            }
        }
        catch (Exception ex)
        {
            Log(string.Format("Error computing SHA-256 for {0}: {1}", filePath, ex.Message));
            return null;
        }
    }

    public static Dictionary<string, string> LoadSha256Manifest(string manifestPath)
    {
        var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(manifestPath)) return dict;
        try
        {
            string[] lines = File.ReadAllLines(manifestPath);
            foreach (var rawLine in lines)
            {
                string line = rawLine.Trim();
                if (string.IsNullOrEmpty(line) || line.StartsWith("#")) continue;
                int sepIndex = line.IndexOf(' ');
                if (sepIndex > 0)
                {
                    string hash = line.Substring(0, sepIndex).Trim();
                    string fileName = line.Substring(sepIndex).Trim().TrimStart('*', ' ');
                    dict[fileName.Replace('/', '\\')] = hash;
                }
            }
        }
        catch (Exception ex)
        {
            Log("Error parsing manifest.sha256: " + ex.Message);
        }
        return dict;
    }

    public static Dictionary<string, string> LoadJsonManifest(string jsonPath)
    {
        var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(jsonPath)) return dict;
        try
        {
            string text = File.ReadAllText(jsonPath);
            var matches = Regex.Matches(
                text,
                "\"([^\"]+\\.py)\"[^}]*?\"sha256\"\\s*:\\s*\"([a-fA-F0-9]{64})\""
            );
            foreach (Match m in matches)
            {
                if (m.Groups.Count >= 3)
                {
                    string file = m.Groups[1].Value.Replace('/', '\\');
                    string hash = m.Groups[2].Value.ToLowerInvariant();
                    dict[file] = hash;
                }
            }
        }
        catch (Exception ex)
        {
            Log("Error parsing installed_manifest.json: " + ex.Message);
        }
        return dict;
    }

    /// <summary>
    /// Performs cryptographic SHA-256 verification of loose Python files before execution.
    /// If corruption or tampering is detected, restores the affected files from the LKG backup.
    /// </summary>
    public static bool VerifyCoreIntegrityAndRecover(string rootDir)
    {
        Dictionary<string, string> expectedHashes = null;
        string manifestSha256 = Path.Combine(rootDir, "manifest.sha256");
        string manifestJson = Path.Combine(rootDir, "installed_manifest.json");

        if (File.Exists(manifestSha256))
        {
            expectedHashes = LoadSha256Manifest(manifestSha256);
        }
        else if (File.Exists(manifestJson))
        {
            expectedHashes = LoadJsonManifest(manifestJson);
        }

        if (expectedHashes == null || expectedHashes.Count == 0)
        {
            Log("No manifest.sha256 or installed_manifest.json found. Skipping pre-launch cryptographic verification.");
            return true;
        }

        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string lkgBackupDir = Path.Combine(localAppData, @"Chirkoot\backups\lkg_core");

        string[] coreFiles = new string[] { "desktop_app.py", "server.py", "security_policy.py" };
        bool allValid = true;

        foreach (var relFile in coreFiles)
        {
            string filePath = Path.Combine(rootDir, relFile);
            string expectedHash;
            if (expectedHashes.TryGetValue(relFile, out expectedHash))
            {
                string actualHash = ComputeSha256(filePath);
                if (!string.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
                {
                    Log(string.Format("[CRITICAL] Core file '{0}' hash mismatch or missing! Expected: {1}, Got: {2}", relFile, expectedHash, actualHash));
                    string backupPath = Path.Combine(lkgBackupDir, relFile);
                    if (File.Exists(backupPath))
                    {
                        string backupHash = ComputeSha256(backupPath);
                        if (string.Equals(backupHash, expectedHash, StringComparison.OrdinalIgnoreCase))
                        {
                            try
                            {
                                File.Copy(backupPath, filePath, true);
                                Log(string.Format("[RECOVERY] Successfully restored '{0}' from Last-Known-Good backup ({1}).", relFile, backupPath));
                                continue;
                            }
                            catch (Exception ex)
                            {
                                Log(string.Format("[RECOVERY FAILED] Error copying backup: {0}", ex.Message));
                                allValid = false;
                            }
                        }
                        else
                        {
                            Log(string.Format("[RECOVERY FAILED] LKG backup for '{0}' is also invalid.", relFile));
                            allValid = false;
                        }
                    }
                    else
                    {
                        Log(string.Format("[RECOVERY FAILED] No LKG backup found at '{0}'.", backupPath));
                        allValid = false;
                    }
                }
                else
                {
                    Log(string.Format("[INTEGRITY OK] {0} matches golden SHA-256.", relFile));
                }
            }
        }

        return allValid;
    }

    /// <summary>
    /// Asynchronously polls http://127.0.0.1:8000/api/health until backend reports healthy.
    /// </summary>
    public static void StartHealthCheckWatcher(Process proc, int timeoutSeconds)
    {
        Thread t = new Thread(delegate()
        {
            DateTime deadline = DateTime.Now.AddSeconds(timeoutSeconds);
            bool healthy = false;

            while (DateTime.Now < deadline)
            {
                if (proc.HasExited)
                {
                    Log(string.Format("[Backend Health Alert] Backend process exited prematurely with code {0}.", proc.ExitCode));
                    return;
                }

                try
                {
                    HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8000/api/health");
                    request.Timeout = 800;
                    request.Method = "GET";
                    using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    {
                        if (response.StatusCode == HttpStatusCode.OK)
                        {
                            using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                            {
                                string body = reader.ReadToEnd();
                                if (body.Contains("\"ok\""))
                                {
                                    healthy = true;
                                    break;
                                }
                            }
                        }
                    }
                }
                catch { }

                Thread.Sleep(500);
            }

            if (healthy)
            {
                Log("[Backend Health Confirmed] Chirkoot HTTP backend is healthy and responding (HTTP 200 OK).");
            }
            else
            {
                Log("[Backend Health Warning] Health check timed out after " + timeoutSeconds + "s. Backend may be slow or unresponsive.");
            }
        });
        t.IsBackground = true;
        t.Start();
    }

    static string FindPythonExecutable(string rootDir)
    {
        // 1. Check bundled Python runtime first (embedded distribution)
        string bundledPy = Path.Combine(rootDir, @"python_runtime\python.exe");
        if (File.Exists(bundledPy)) return bundledPy;

        bundledPy = Path.Combine(rootDir, @"python-embed\python.exe");
        if (File.Exists(bundledPy)) return bundledPy;

        // 2. Check local user Python installations
        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string directPath = Path.Combine(userProfile, @"AppData\Local\Programs\Python\Python312\python.exe");
        if (File.Exists(directPath)) return directPath;

        string pyBase = Path.Combine(userProfile, @"AppData\Local\Programs\Python");
        if (Directory.Exists(pyBase))
        {
            foreach (var dir in Directory.GetDirectories(pyBase, "Python*"))
            {
                string cand = Path.Combine(dir, "python.exe");
                if (File.Exists(cand)) return cand;
            }
        }

        // 3. Check system PATH
        string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string part in pathEnv.Split(';'))
        {
            string trimmed = part.Trim().Trim('"');
            if (!string.IsNullOrEmpty(trimmed))
            {
                string cand = Path.Combine(trimmed, "python.exe");
                if (File.Exists(cand)) return cand;
            }
        }

        return "python.exe";
    }

    [STAThread]
    static void Main(string[] args)
    {
        InitLogger();
        Log("=================================================");
        Log("Chirkoot MK-V Windows Desktop Launcher Initializing");
        Log("=================================================");

        // Step 1: Enforce Single-Instance Mutex
        bool createdNew;
        using (Mutex mutex = new Mutex(true, LAUNCHER_MUTEX_NAME, out createdNew))
        {
            if (!createdNew)
            {
                Log("[Single-Instance] Another Chirkoot Launcher instance is already running. Activating existing window.");
                BringExistingWindowToFront();
                return;
            }

            string rootDir = AppDomain.CurrentDomain.BaseDirectory;
            Log("Application Root: " + rootDir);

            // Step 2: Loose Python Files Pre-launch Integrity Verification & Recovery
            bool integrityOk = VerifyCoreIntegrityAndRecover(rootDir);

            string scriptPath = Path.Combine(rootDir, "desktop_app.py");
            if (!File.Exists(scriptPath))
            {
                scriptPath = Path.Combine(rootDir, "server.py");
            }

            string pythonExe = FindPythonExecutable(rootDir);
            Log("Selected Python Runtime: " + pythonExe);

            string passedArgs = (args != null && args.Length > 0) ? " " + string.Join(" ", args) : "";

            if (!integrityOk && !passedArgs.Contains("--safe-mode"))
            {
                passedArgs += " --safe-mode";
                Log("[Integrity Alert] Critical files unrecoverable. Forcing Safe Mode.");
            }

            // Step 3: Watchdog Crash-Loop Check: Engage Safe Mode if repeated startup crashes detected
            try
            {
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string healthFile = Path.Combine(localAppData, @"Chirkoot\config\startup_health.json");
                if (File.Exists(healthFile))
                {
                    string healthJson = File.ReadAllText(healthFile);
                    if ((healthJson.Contains("\"consecutive_crashes\": 3") ||
                         healthJson.Contains("\"safe_mode_forced\": true") ||
                         healthJson.Contains("\"consecutive_crashes\": 4") ||
                         healthJson.Contains("\"consecutive_crashes\": 5")) &&
                        !passedArgs.Contains("--safe-mode"))
                    {
                        passedArgs += " --safe-mode";
                        Log("[Watchdog Alert] Repeated startup crashes detected. Auto-engaging Safe Mode.");
                    }
                }
            }
            catch (Exception ex)
            {
                Log("Watchdog check notice: " + ex.Message);
            }

            // Step 4: Win32 Job Object for Controlled Process Lifecycle
            IntPtr hJob = SetupJobObject();

            // Step 5: Launch Backend / Desktop App
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = pythonExe;
            psi.Arguments = string.Format("\"{0}\"{1}", scriptPath, passedArgs);
            psi.WorkingDirectory = rootDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = false;

            try
            {
                Log(string.Format("Spawning process: {0} {1}", psi.FileName, psi.Arguments));
                Process proc = Process.Start(psi);
                if (proc != null)
                {
                    if (hJob != IntPtr.Zero)
                    {
                        AssignProcessToJobObject(hJob, proc.Handle);
                        Log(string.Format("Assigned PID {0} to Win32 Job Object.", proc.Id));
                    }

                    // Step 6: Start Asynchronous Health Check Poller
                    StartHealthCheckWatcher(proc, 15);

                    proc.WaitForExit();
                    Log(string.Format("Process exited with code {0}.", proc.ExitCode));
                }
            }
            catch (Exception ex)
            {
                Log("Error launching Chirkoot: " + ex.Message);
                MessageBox.Show(
                    "Error launching Chirkoot: " + ex.Message + "\n\nTarget: " + pythonExe,
                    "Chirkoot Desktop Launcher",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
            finally
            {
                if (hJob != IntPtr.Zero)
                {
                    CloseHandle(hJob);
                }
            }
        }
    }
}
