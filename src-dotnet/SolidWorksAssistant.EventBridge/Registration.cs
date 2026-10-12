using System;
using Microsoft.Win32;

namespace SolidWorksAssistant.EventBridge
{
    internal static class Registration
    {
        private const string MachineAddins = @"SOFTWARE\SOLIDWORKS\Addins";
        private const string UserStartup = @"Software\SOLIDWORKS\AddInsStartup";

        public static void Register(Type type)
        {
            string guid = "{" + type.GUID.ToString().ToUpperInvariant() + "}";
            using (RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
            using (RegistryKey key = baseKey.CreateSubKey(MachineAddins + "\\" + guid, true))
            {
                if (key == null)
                {
                    throw new InvalidOperationException("Unable to create the SOLIDWORKS add-in registry key.");
                }

                key.SetValue(null, 0, RegistryValueKind.DWord);
                key.SetValue("Title", BridgeConstants.AddinTitle, RegistryValueKind.String);
                key.SetValue("Description", BridgeConstants.AddinDescription, RegistryValueKind.String);
            }

            using (RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry64))
            using (RegistryKey parent = baseKey.CreateSubKey(UserStartup, true))
            using (RegistryKey key = baseKey.CreateSubKey(UserStartup + "\\" + guid, true))
            {
                if (parent != null)
                {
                    parent.DeleteValue(guid, false);
                }
                if (key != null)
                {
                    key.SetValue(null, 1, RegistryValueKind.DWord);
                }
            }
        }

        public static void Unregister(Type type)
        {
            string guid = "{" + type.GUID.ToString().ToUpperInvariant() + "}";
            using (RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
            {
                baseKey.DeleteSubKeyTree(MachineAddins + "\\" + guid, false);
            }

            using (RegistryKey baseKey = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry64))
            using (RegistryKey key = baseKey.OpenSubKey(UserStartup, true))
            {
                if (key != null)
                {
                    key.DeleteValue(guid, false);
                    key.DeleteSubKeyTree(guid, false);
                }
            }
        }
    }
}
