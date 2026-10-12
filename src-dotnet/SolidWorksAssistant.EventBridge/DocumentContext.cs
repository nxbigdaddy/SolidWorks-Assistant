using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;

namespace SolidWorksAssistant.EventBridge
{
    public static class DocumentContextReader
    {
        public static IDictionary<string, object> Read(
            SldWorks application,
            ModelDoc2 document,
            string solidWorksRevision,
            bool commandInProgress)
        {
            Dictionary<string, object> context = new Dictionary<string, object>
            {
                { "captured_at", DateTime.UtcNow.ToString("o") },
                { "process_id", System.Diagnostics.Process.GetCurrentProcess().Id },
                { "solidworks_revision", solidWorksRevision ?? string.Empty },
                { "command_in_progress", commandInProgress },
                { "document", ReadDocument(document) },
                { "selections", ReadSelections(document) },
                { "stale_after_ms", 15000 },
            };
            return context;
        }

        public static IDictionary<string, object> ReadDocument(ModelDoc2 document)
        {
            if (document == null)
            {
                return new Dictionary<string, object>();
            }

            try
            {
                string configuration = string.Empty;
                try
                {
                    ConfigurationManager manager = document.ConfigurationManager;
                    Configuration active = manager == null ? null : manager.ActiveConfiguration;
                    configuration = active == null ? string.Empty : active.Name;
                }
                catch (COMException)
                {
                    configuration = string.Empty;
                }

                return new Dictionary<string, object>
                {
                    { "title", document.GetTitle() ?? string.Empty },
                    { "path", document.GetPathName() ?? string.Empty },
                    { "document_type", document.GetType() },
                    { "configuration", configuration ?? string.Empty },
                    { "save_pending", document.GetSaveFlag() },
                    { "read_only", SafeReadOnly(document) },
                    { "update_stamp", document.GetUpdateStamp() },
                };
            }
            catch (COMException exception)
            {
                return new Dictionary<string, object> { { "read_error", exception.ErrorCode } };
            }
        }

        public static IList<IDictionary<string, object>> ReadSelections(ModelDoc2 document)
        {
            List<IDictionary<string, object>> selections = new List<IDictionary<string, object>>();
            if (document == null)
            {
                return selections;
            }

            try
            {
                SelectionMgr manager = document.ISelectionManager;
                if (manager == null)
                {
                    return selections;
                }

                int count = manager.GetSelectedObjectCount2(-1);
                for (int index = 1; index <= count; index++)
                {
                    int selectionType = SafeSelectionType(manager, index);
                    object selected = SafeSelectedObject(manager, index);
                    Component2 component = SafeSelectedComponent(manager, index);
                    selections.Add(new Dictionary<string, object>
                    {
                        { "index", index },
                        { "selection_type", selectionType },
                        { "type_name", SelectionTypeName(selectionType) },
                        { "name", SafeObjectName(selected) },
                        { "component_path", SafeComponentPath(component) },
                        { "persistent_ref", SafePersistentReference(document, selected) },
                    });
                }
            }
            catch (COMException exception)
            {
                selections.Add(new Dictionary<string, object>
                {
                    { "read_error", exception.ErrorCode },
                });
            }
            return selections;
        }

        private static bool SafeReadOnly(ModelDoc2 document)
        {
            try
            {
                return document.IsOpenedReadOnly();
            }
            catch (COMException)
            {
                return true;
            }
        }

        private static int SafeSelectionType(SelectionMgr manager, int index)
        {
            try
            {
                return manager.GetSelectedObjectType3(index, -1);
            }
            catch (COMException)
            {
                return 0;
            }
        }

        private static object SafeSelectedObject(SelectionMgr manager, int index)
        {
            try
            {
                return manager.GetSelectedObject6(index, -1);
            }
            catch (COMException)
            {
                return null;
            }
        }

        private static Component2 SafeSelectedComponent(SelectionMgr manager, int index)
        {
            try
            {
                return manager.GetSelectedObjectsComponent4(index, -1) as Component2;
            }
            catch (COMException)
            {
                return null;
            }
        }

        private static string SafeComponentPath(Component2 component)
        {
            if (component == null)
            {
                return string.Empty;
            }
            try
            {
                return component.GetPathName() ?? string.Empty;
            }
            catch (COMException)
            {
                return string.Empty;
            }
        }

        private static string SafeObjectName(object selected)
        {
            if (selected == null)
            {
                return string.Empty;
            }
            try
            {
                Feature feature = selected as Feature;
                if (feature != null)
                {
                    return feature.Name ?? string.Empty;
                }
                Component2 component = selected as Component2;
                if (component != null)
                {
                    return component.Name2 ?? string.Empty;
                }
                ModelDoc2 model = selected as ModelDoc2;
                if (model != null)
                {
                    return model.GetTitle() ?? string.Empty;
                }
            }
            catch (COMException)
            {
                return string.Empty;
            }
            return string.Empty;
        }

        private static string SafePersistentReference(ModelDoc2 document, object selected)
        {
            if (selected == null)
            {
                return string.Empty;
            }
            try
            {
                object value = document.Extension.GetPersistReference3(selected);
                byte[] bytes = value as byte[];
                if (bytes == null && value is Array array)
                {
                    bytes = new byte[array.Length];
                    for (int index = 0; index < array.Length; index++)
                    {
                        bytes[index] = Convert.ToByte(array.GetValue(index));
                    }
                }
                return bytes == null ? string.Empty : Convert.ToBase64String(bytes);
            }
            catch (Exception exception) when (exception is COMException || exception is InvalidCastException || exception is FormatException)
            {
                return string.Empty;
            }
        }

        public static string SelectionTypeName(int selectionType)
        {
            switch (selectionType)
            {
                case 1: return "edge";
                case 2: return "face";
                case 3: return "vertex";
                case 4: return "datum_plane";
                case 5: return "datum_axis";
                case 6: return "datum_point";
                case 7: return "sketch";
                case 8: return "sketch_segment";
                case 9: return "sketch_point";
                case 20: return "component";
                default: return "selection_" + selectionType;
            }
        }
    }
}
