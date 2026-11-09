using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

namespace ShareBoxSetup
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            bool silent = Array.IndexOf(args, "--silent") >= 0;
            string target = null;
            for (int i = 0; i < args.Length - 1; i++)
                if (args[i] == "--target") target = args[i + 1];

            string defaultTarget = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "ShareBoxApp");

            if (silent)
            {
                try
                {
                    string used = Installer.InstallWithFallback(target ?? defaultTarget, true, false);
                    Installer.Log("silent installed at " + used);
                    Environment.Exit(0);
                }
                catch (Exception ex)
                {
                    Installer.Log("SILENT INSTALL FAILED: " + ex);
                    Environment.Exit(1);
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new InstallerForm(defaultTarget));
        }
    }

    static class Installer
    {
        public const string Version = "2.1.8";
        public const string Payload = "payload.zip";

        public static string LogPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ShareBox", "install.log");
            }
        }

        public static void Log(string text)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath));
                File.AppendAllText(LogPath,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + text + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch { }
        }

        public static void KillRunning()
        {
            foreach (var p in Process.GetProcessesByName("ShareBox"))
            {
                try { p.Kill(); p.WaitForExit(3000); } catch { }
            }
        }

        public static void Install(string target, bool desktop, bool launch)
        {
            string error;
            if (!CanWrite(target, out error))
            {
                throw new Exception("无法写入安装目录：\n" + target + "\n\n" + error +
                    "\n\n请点「更改…」选择其他位置，例如 D:\\ShareBox。");
            }
            Log("install start -> " + target);
            Directory.CreateDirectory(target);
            KillRunning();
            Extract(target);
            Log("extracted");
            CreateShortcuts(target, desktop);
            File.WriteAllText(Path.Combine(target, "install.json"),
                "{\"version\":\"" + Version + "\",\"installed_at\":\"" + DateTime.Now.ToString("s") + "\"}",
                new UTF8Encoding(false));
            WriteUninstaller(target);
            Log("shortcuts + uninstaller done");
            if (launch)
            {
                string exe = Path.Combine(target, "ShareBox.exe");
                if (File.Exists(exe))
                {
                    try { Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = target }); }
                    catch (Exception ex) { Log("launch failed: " + ex.Message); }
                }
            }
            Log("install success");
        }

        public static bool CanWrite(string target, out string error)
        {
            try
            {
                Directory.CreateDirectory(target);
                string probe = Path.Combine(target, ".sharebox-write-test");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
                error = null;
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        public static string SuggestTarget(string preferred)
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string[] candidates = new string[]
            {
                preferred,
                Path.Combine(local, "ShareBoxApp"),
                Path.Combine(profile, "ShareBoxApp"),
                @"D:\ShareBox"
            };
            foreach (string candidate in candidates)
            {
                string error;
                if (CanWrite(candidate, out error)) return candidate;
            }
            return preferred;
        }

        public static string InstallWithFallback(string preferred, bool desktop, bool launch)
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string[] candidates = new string[]
            {
                preferred,
                Path.Combine(local, "ShareBoxApp"),
                Path.Combine(profile, "ShareBoxApp"),
                @"D:\ShareBox"
            };
            Exception last = null;
            foreach (string candidate in candidates)
            {
                string error;
                if (!CanWrite(candidate, out error))
                {
                    last = new Exception(candidate + "：" + error);
                    continue;
                }
                try
                {
                    Install(candidate, desktop, launch);
                    Log("installed at " + candidate);
                    return candidate;
                }
                catch (Exception ex)
                {
                    last = ex;
                    Log("install failed at " + candidate + ": " + ex.Message);
                }
            }
            throw new Exception("没有找到可写的安装目录。\n" + (last == null ? "" : last.Message));
        }

        static void Extract(string target)
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(Payload))
            {
                if (stream == null) throw new Exception("安装包内缺少程序文件（payload.zip）");
                using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    foreach (var entry in zip.Entries)
                    {
                        string name = entry.FullName.Replace('\\', '/');
                        bool isDir = name.EndsWith("/") || string.IsNullOrEmpty(entry.Name);
                        string rel = name.TrimEnd('/').Replace('/', Path.DirectorySeparatorChar);
                        if (rel.Length == 0) continue;
                        string dest = Path.Combine(target, rel);
                        if (isDir)
                        {
                            Directory.CreateDirectory(dest);
                            continue;
                        }
                        string dir = Path.GetDirectoryName(dest);
                        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                        using (Stream src = entry.Open())
                        using (FileStream dst = File.Create(dest))
                            src.CopyTo(dst);
                    }
                }
            }
        }

        static void CreateShortcuts(string target, bool desktop)
        {
            string exe = Path.Combine(target, "ShareBox.exe");
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs\ShareBox");
            Directory.CreateDirectory(startMenu);
            Shortcut(Path.Combine(startMenu, "ShareBox.lnk"), exe, target, "ShareBox 共享夹");
            Shortcut(Path.Combine(startMenu, "卸载 ShareBox.lnk"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ShareBox", "uninstall.cmd"), "", "卸载 ShareBox");

            string desktopLnk = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "ShareBox.lnk");
            if (desktop) Shortcut(desktopLnk, exe, target, "ShareBox 共享夹");
            else { try { File.Delete(desktopLnk); } catch { } }

            string startup = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs\Startup\ShareBox.lnk");
            try { if (File.Exists(startup)) File.Delete(startup); } catch { }
        }

        static void Shortcut(string lnk, string targetPath, string workDir, string desc)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType == null) return;
            dynamic shell = Activator.CreateInstance(shellType);
            dynamic sc = shell.CreateShortcut(lnk);
            sc.TargetPath = targetPath;
            if (!string.IsNullOrEmpty(workDir)) sc.WorkingDirectory = workDir;
            sc.Description = desc;
            if (File.Exists(targetPath)) sc.IconLocation = targetPath + ",0";
            sc.Save();
        }

        static void WriteUninstaller(string target)
        {
            string dataDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ShareBox");
            Directory.CreateDirectory(dataDir);
            string cmd = Path.Combine(dataDir, "uninstall.cmd");
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                @"Microsoft\Windows\Start Menu\Programs\ShareBox");
            string desktopLnk = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "ShareBox.lnk");

            var sb = new StringBuilder();
            sb.AppendLine("@echo off");
            sb.AppendLine("taskkill /IM ShareBox.exe /F >nul 2>nul");
            sb.AppendLine("timeout /t 1 /nobreak >nul");
            sb.AppendLine("rmdir /s /q \"" + target + "\"");
            sb.AppendLine("rmdir /s /q \"" + startMenu + "\"");
            sb.AppendLine("del \"" + desktopLnk + "\" >nul 2>nul");
            sb.AppendLine("reg delete \"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\ShareBox\" /f >nul 2>nul");
            sb.AppendLine("start /b cmd /c del \"%~f0\"");
            File.WriteAllText(cmd, sb.ToString(), Encoding.Default);

            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Uninstall\ShareBox"))
            {
                key.SetValue("DisplayName", "ShareBox 共享夹");
                key.SetValue("DisplayVersion", Version);
                key.SetValue("InstallLocation", target);
                key.SetValue("DisplayIcon", Path.Combine(target, "ShareBox.exe"));
                key.SetValue("UninstallString", "\"" + cmd + "\"");
                key.SetValue("Publisher", "ShareBox");
                key.SetValue("NoModify", 1);
                key.SetValue("NoRepair", 1);
            }
        }
    }

    class InstallerForm : Form
    {
        TextBox pathBox;
        CheckBox desktopBox;
        Label status;
        Button installButton;

        public InstallerForm(string defaultTarget)
        {
            Text = "共享夹 ShareBox - 安装程序";
            ClientSize = new Size(660, 460);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            BackColor = Color.White;
            Font = new Font("Microsoft YaHei UI", 9F);

            string writable = Installer.SuggestTarget(defaultTarget);
            string fallbackNote = writable == defaultTarget ? "" :
                "默认位置没有写入权限，已自动改为：" + writable;

            var header = new Panel { Dock = DockStyle.Top, Height = 96, BackColor = Color.FromArgb(59, 91, 219) };
            header.Controls.Add(new Label
            {
                Text = "共享夹 ShareBox",
                ForeColor = Color.White,
                Font = new Font("Microsoft YaHei UI", 18F, FontStyle.Bold),
                AutoSize = true,
                Location = new Point(24, 18)
            });
            header.Controls.Add(new Label
            {
                Text = "同事之间直接共享文件夹 · 公司隔离 · 安装程序 " + Installer.Version,
                ForeColor = Color.FromArgb(219, 228, 255),
                AutoSize = true,
                Location = new Point(27, 62)
            });
            Controls.Add(header);

            var body = new Panel { Dock = DockStyle.Fill, Padding = new Padding(26, 18, 26, 10) };
            Controls.Add(body);

            var lbl = new Label { Text = "安装位置", AutoSize = true, ForeColor = Color.FromArgb(91, 100, 120) };
            body.Controls.Add(lbl);
            pathBox = new TextBox { Text = writable, Left = 0, Top = 26, Width = 520 };
            body.Controls.Add(pathBox);
            var browse = new Button { Text = "更改…", Left = 530, Top = 24, Width = 72, Height = 28 };
            browse.Click += delegate
            {
                using (var dialog = new FolderBrowserDialog())
                {
                    dialog.Description = "选择安装位置";
                    if (dialog.ShowDialog() == DialogResult.OK)
                        pathBox.Text = Path.Combine(dialog.SelectedPath, "ShareBox");
                }
            };
            body.Controls.Add(browse);

            desktopBox = new CheckBox
            {
                Text = "创建桌面快捷方式",
                Checked = true,
                AutoSize = true,
                Left = 0,
                Top = 66
            };
            body.Controls.Add(desktopBox);

            status = new Label
            {
                Text = "点击「立即安装」开始。安装完成后会打开控制面板。",
                AutoSize = false,
                Left = 0,
                Top = 104,
                Width = 600,
                Height = 44,
                ForeColor = Color.FromArgb(91, 100, 120)
            };
            body.Controls.Add(status);

            installButton = new Button
            {
                Text = "立即安装",
                Left = 470,
                Top = 170,
                Width = 132,
                Height = 40,
                BackColor = Color.FromArgb(59, 91, 219),
                ForeColor = Color.White,
                FlatStyle = FlatStyle.Flat
            };
            installButton.FlatAppearance.BorderSize = 0;
            installButton.Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold);
            installButton.Click += delegate { DoInstall(); };
            body.Controls.Add(installButton);

            var note = new Label
            {
                Text = fallbackNote.Length > 0 ? fallbackNote :
                    "安装到当前用户目录，不需要管理员权限；如果提示拒绝访问，请点「更改…」换到 D:\\ShareBox。",
                AutoSize = false,
                Left = 0,
                Top = 180,
                Width = 600,
                Height = 40,
                ForeColor = fallbackNote.Length > 0 ? Color.FromArgb(180, 90, 30) : Color.Gray
            };
            body.Controls.Add(note);
        }

        void DoInstall()
        {
            string target = pathBox.Text.Trim();
            if (target.Length == 0)
            {
                MessageBox.Show("请填写安装位置", "提示", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            installButton.Enabled = false;
            installButton.Text = "正在安装…";
            status.Text = "正在释放程序文件（约 33 MB），请稍候…";
            Application.DoEvents();
            try
            {
                string used = Installer.InstallWithFallback(target, desktopBox.Checked, false);
                status.Text = "安装完成，正在启动…";
                Application.DoEvents();
                string exe = Path.Combine(used, "ShareBox.exe");
                if (File.Exists(exe))
                    Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = used });
                string extra = used == target ? "" :
                    "\n\n原目录不可写，已自动安装到：\n" + used;
                MessageBox.Show("安装完成。" + extra +
                    "\n\n关闭控制面板后程序会停止；再次打开请点桌面「ShareBox」。",
                    "安装完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
                Close();
            }
            catch (Exception ex)
            {
                Installer.Log("INTERACTIVE INSTALL FAILED: " + ex);
                status.Text = "安装失败：" + ex.Message;
                MessageBox.Show("安装失败：\n" + ex.Message + "\n\n详细信息已写入：\n" + Installer.LogPath,
                    "安装失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                installButton.Enabled = true;
                installButton.Text = "立即安装";
            }
        }
    }
}
