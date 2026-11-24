using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

class GeminiLiveLauncher
{
    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    private const int SW_RESTORE = 9;

    static string FindPythonExecutable(string rootDir)
    {
        // 1. Direct path for user's Python 3.12 installation
        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string directPath = Path.Combine(userProfile, @"AppData\Local\Programs\Python\Python312\python.exe");
        if (File.Exists(directPath)) return directPath;

        // 2. Scan AppData\Local\Programs\Python\Python*
        string pyBase = Path.Combine(userProfile, @"AppData\Local\Programs\Python");
        if (Directory.Exists(pyBase))
        {
            foreach (var dir in Directory.GetDirectories(pyBase, "Python*"))
            {
                string cand = Path.Combine(dir, "python.exe");
                if (File.Exists(cand)) return cand;
            }
        }

        // 3. Check PATH
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

        // 4. Fallback
        return "python.exe";
    }

    [STAThread]
    static void Main(string[] args)
    {
        string rootDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = Path.Combine(rootDir, "desktop_app.py");

        if (!File.Exists(scriptPath))
        {
            // Check fallback
            scriptPath = Path.Combine(rootDir, "server.py");
        }

        string pythonExe = FindPythonExecutable(rootDir);

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pythonExe;
        psi.Arguments = string.Format("\"{0}\"", scriptPath);
        psi.WorkingDirectory = rootDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = false;

        try
        {
            Process proc = Process.Start(psi);
            proc.WaitForExit();
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                "Error launching Gemini Live: " + ex.Message + "\n\nTarget: " + pythonExe,
                "Gemini Live Launcher",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }
}
