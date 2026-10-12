using System;
using System.IO;

namespace SolidWorksAssistant.EventBridge
{
    public sealed class BridgePaths
    {
        public BridgePaths(string root)
        {
            if (string.IsNullOrWhiteSpace(root))
            {
                throw new ArgumentException("Bridge root must not be empty.", "root");
            }

            Root = Path.GetFullPath(root);
            Events = Path.Combine(Root, "events.jsonl");
            Capabilities = Path.Combine(Root, "capabilities.json");
            Diagnostics = Path.Combine(Root, "diagnostics.log");
        }

        public string Root { get; private set; }

        public string Events { get; private set; }

        public string Capabilities { get; private set; }

        public string Diagnostics { get; private set; }

        public static BridgePaths Default()
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return new BridgePaths(Path.Combine(local, "SolidWorksAIAssistant", "event-bridge"));
        }

        public void Ensure()
        {
            Directory.CreateDirectory(Root);
        }
    }
}
