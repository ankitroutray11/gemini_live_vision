using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

class ChitrakootLauncher
{
    static string FindPythonExecutable(string rootDir)
    {
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
        string rootDir = AppDomain.CurrentDomain.BaseDirectory;
        string scriptPath = Path.Combine(rootDir, "chitrakoot_app.py");

        if (!File.Exists(scriptPath))
        {
            scriptPath = Path.Combine(rootDir, "chitrakoot_server.py");
        }

        string pythonExe = FindPythonExecutable(rootDir);

        string passedArgs = (args != null && args.Length > 0) ? " " + string.Join(" ", args) : "";

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pythonExe;
        psi.Arguments = string.Format("\"{0}\"{1}", scriptPath, passedArgs);
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
                "Error launching Chitrakoot Neural Core: " + ex.Message + "\n\nTarget: " + pythonExe,
                "Chitrakoot Offline AI Launcher",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }
}
