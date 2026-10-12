using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Drawing.Imaging;
using SolidWorks.Interop.sldworks;

namespace SolidWorksAssistant.EventBridge
{
    public sealed class AssistantTaskPaneControl : UserControl
    {
        private readonly Label connectionLabel;
        private readonly Label documentLabel;
        private readonly Label configurationLabel;
        private readonly Label selectionLabel;
        private readonly Label messageLabel;
        private readonly Button launchButton;

        public AssistantTaskPaneControl()
        {
            AutoScaleMode = AutoScaleMode.Dpi;
            BackColor = Color.FromArgb(245, 247, 250);
            ForeColor = Color.FromArgb(31, 41, 55);
            Padding = new Padding(12);

            TableLayoutPanel layout = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 8,
                BackColor = BackColor,
                AutoScroll = true,
            };
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            Label title = LabelFor("AI 智能体", 14F, true);
            Label subtitle = LabelFor("SolidWorks 助手", 9F, false);
            subtitle.ForeColor = Color.FromArgb(107, 114, 128);
            connectionLabel = LabelFor("事件桥：正在连接", 9F, true);
            documentLabel = LabelFor("文档：无", 9F, false);
            configurationLabel = LabelFor("配置：无", 9F, false);
            selectionLabel = LabelFor("选择：0 项", 9F, false);
            launchButton = new Button
            {
                Text = "打开完整助手",
                AutoSize = true,
                FlatStyle = FlatStyle.Flat,
                BackColor = Color.FromArgb(37, 99, 235),
                ForeColor = Color.White,
                Margin = new Padding(0, 12, 0, 6),
                Padding = new Padding(8, 4, 8, 4),
                Cursor = Cursors.Hand,
            };
            launchButton.FlatAppearance.BorderSize = 0;
            launchButton.Click += OnLaunch;
            messageLabel = LabelFor("选择模型对象后，上下文会自动同步。", 8.5F, false);
            messageLabel.ForeColor = Color.FromArgb(107, 114, 128);

            layout.Controls.Add(title, 0, 0);
            layout.Controls.Add(subtitle, 0, 1);
            layout.Controls.Add(connectionLabel, 0, 2);
            layout.Controls.Add(documentLabel, 0, 3);
            layout.Controls.Add(configurationLabel, 0, 4);
            layout.Controls.Add(selectionLabel, 0, 5);
            layout.Controls.Add(launchButton, 0, 6);
            layout.Controls.Add(messageLabel, 0, 7);
            Controls.Add(layout);
        }

        public void SetConnected(bool connected, string revision)
        {
            InvokeOnUi(delegate
            {
                connectionLabel.Text = connected
                    ? "事件桥：已连接 · SolidWorks " + (revision ?? string.Empty)
                    : "事件桥：未连接";
                connectionLabel.ForeColor = connected
                    ? Color.FromArgb(22, 101, 52)
                    : Color.FromArgb(185, 28, 28);
            });
        }

        public void UpdateContext(IDictionary<string, object> context)
        {
            InvokeOnUi(delegate
            {
                IDictionary<string, object> document = ValueAsDictionary(context, "document");
                string title = ValueAsString(document, "title");
                string configuration = ValueAsString(document, "configuration");
                int selectionCount = ValueAsListCount(context, "selections");
                documentLabel.Text = "文档：" + (string.IsNullOrWhiteSpace(title) ? "无" : title);
                configurationLabel.Text = "配置：" + (
                    string.IsNullOrWhiteSpace(configuration) ? "无" : configuration);
                selectionLabel.Text = "选择：" + selectionCount + " 项";
                bool busy = ValueAsBoolean(context, "command_in_progress");
                messageLabel.Text = busy
                    ? "SolidWorks 正在执行命令，写操作暂时锁定。"
                    : "上下文已同步；完整计划和恢复操作在桌面助手中执行。";
            });
        }

        public void SetMessage(string message, bool error)
        {
            InvokeOnUi(delegate
            {
                messageLabel.Text = message ?? string.Empty;
                messageLabel.ForeColor = error
                    ? Color.FromArgb(185, 28, 28)
                    : Color.FromArgb(22, 101, 52);
            });
        }

        private void OnLaunch(object sender, EventArgs eventArgs)
        {
            string message;
            bool launched = DesktopAssistantLauncher.TryLaunch(out message);
            SetMessage(message, !launched);
        }

        private void InvokeOnUi(Action action)
        {
            if (IsDisposed)
            {
                return;
            }
            if (InvokeRequired)
            {
                BeginInvoke(action);
            }
            else
            {
                action();
            }
        }

        private static Label LabelFor(string text, float size, bool bold)
        {
            return new Label
            {
                Text = text,
                AutoSize = true,
                MaximumSize = new Size(320, 0),
                Margin = new Padding(0, 2, 0, 4),
                Font = new Font("Microsoft YaHei UI", size, bold ? FontStyle.Bold : FontStyle.Regular),
            };
        }

        private static IDictionary<string, object> ValueAsDictionary(
            IDictionary<string, object> source,
            string key)
        {
            if (source != null && source.TryGetValue(key, out object value))
            {
                return value as IDictionary<string, object> ?? new Dictionary<string, object>();
            }
            return new Dictionary<string, object>();
        }

        private static string ValueAsString(IDictionary<string, object> source, string key)
        {
            return source != null && source.TryGetValue(key, out object value)
                ? Convert.ToString(value) ?? string.Empty
                : string.Empty;
        }

        private static bool ValueAsBoolean(IDictionary<string, object> source, string key)
        {
            return source != null && source.TryGetValue(key, out object value) && Convert.ToBoolean(value);
        }

        private static int ValueAsListCount(IDictionary<string, object> source, string key)
        {
            if (source != null && source.TryGetValue(key, out object value) && value is System.Collections.ICollection collection)
            {
                return collection.Count;
            }
            return 0;
        }
    }

    public sealed class AssistantTaskPaneHost : IDisposable
    {
        private readonly TaskpaneView view;
        private readonly AssistantTaskPaneControl control;
        private bool disposed;

        public AssistantTaskPaneHost(SldWorks application)
        {
            if (application == null)
            {
                throw new ArgumentNullException(nameof(application));
            }
            string iconPath = TaskPaneIcon.Ensure();
            TaskpaneView created = application.CreateTaskpaneView3(
                new string[] { iconPath },
                "AI 智能体（SolidWorks 助手）");
            if (created == null)
            {
                created = application.CreateTaskpaneView2(
                    iconPath,
                    "AI 智能体（SolidWorks 助手）");
            }
            view = created;
            if (view == null)
            {
                throw new InvalidOperationException("SolidWorks 没有创建 Task Pane。 ");
            }
            control = new AssistantTaskPaneControl();
            control.CreateControl();
            if (!view.DisplayWindowFromHandlex64(control.Handle.ToInt64()))
            {
                control.Dispose();
                view.DeleteView();
                throw new InvalidOperationException("SolidWorks 拒绝托管 Task Pane 窗口。 ");
            }
            view.ShowView();
        }

        public void SetConnected(bool connected, string revision)
        {
            control.SetConnected(connected, revision);
        }

        public void UpdateContext(IDictionary<string, object> context)
        {
            control.UpdateContext(context);
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
            try
            {
                view.DeleteView();
            }
            catch (COMException)
            {
            }
            control.Dispose();
        }
    }

    internal static class TaskPaneIcon
    {
        public static string Ensure()
        {
            BridgePaths paths = BridgePaths.Default();
            paths.Ensure();
            string iconPath = Path.Combine(paths.Root, "taskpane-icon.bmp");
            if (File.Exists(iconPath))
            {
                return iconPath;
            }
            using (Bitmap bitmap = new Bitmap(16, 18, PixelFormat.Format24bppRgb))
            using (Graphics graphics = Graphics.FromImage(bitmap))
            using (Brush blue = new SolidBrush(Color.FromArgb(37, 99, 235)))
            using (Brush cyan = new SolidBrush(Color.FromArgb(34, 211, 238)))
            {
                graphics.Clear(Color.White);
                graphics.FillRectangle(blue, 2, 3, 12, 12);
                graphics.FillEllipse(cyan, 5, 6, 6, 6);
                bitmap.Save(iconPath, ImageFormat.Bmp);
            }
            return iconPath;
        }
    }

    public static class DesktopAssistantLauncher
    {
        public static bool TryLaunch(out string message)
        {
            foreach (string candidate in Candidates())
            {
                if (!File.Exists(candidate))
                {
                    continue;
                }
                try
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = candidate,
                        WorkingDirectory = Path.GetDirectoryName(candidate),
                        UseShellExecute = true,
                    });
                    message = "完整助手已启动。";
                    return true;
                }
                catch (Exception exception) when (exception is InvalidOperationException || exception is System.ComponentModel.Win32Exception)
                {
                    message = "启动完整助手失败：" + exception.Message;
                    return false;
                }
            }
            message = "没有找到完整助手。请设置 SW_ASSISTANT_EXE 或 SW_ASSISTANT_HOME。";
            return false;
        }

        private static IEnumerable<string> Candidates()
        {
            string executable = System.Environment.GetEnvironmentVariable("SW_ASSISTANT_EXE");
            if (!string.IsNullOrWhiteSpace(executable))
            {
                yield return Path.GetFullPath(executable);
            }
            string home = System.Environment.GetEnvironmentVariable("SW_ASSISTANT_HOME");
            if (!string.IsNullOrWhiteSpace(home))
            {
                yield return Path.Combine(Path.GetFullPath(home), "SolidWorksAssistant.exe");
                yield return Path.Combine(Path.GetFullPath(home), "run.bat");
            }
            string assembly = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            if (!string.IsNullOrWhiteSpace(assembly))
            {
                yield return Path.Combine(assembly, "SolidWorksAssistant.exe");
            }
        }
    }
}
