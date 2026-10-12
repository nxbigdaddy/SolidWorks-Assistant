using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;
using SolidWorks.Interop.sldworks;

namespace SolidWorksAssistant.EventBridge
{
    public sealed class SolidWorksVersion
    {
        public string Revision { get; set; }

        public int MajorRevision { get; set; }

        public int ServicePack { get; set; }

        public int ReleaseYear { get; set; }

        public static SolidWorksVersion Parse(string revision)
        {
            string safe = revision ?? string.Empty;
            string[] parts = safe.Split('.');
            int major;
            int servicePack;
            int.TryParse(parts.Length > 0 ? parts[0] : "0", out major);
            int.TryParse(parts.Length > 1 ? parts[1] : "0", out servicePack);
            return new SolidWorksVersion
            {
                Revision = safe,
                MajorRevision = major,
                ServicePack = servicePack,
                ReleaseYear = major > 0 ? 1992 + major : 0,
            };
        }
    }

    public sealed class CapabilityResult
    {
        public string Id { get; set; }

        public string Category { get; set; }

        public string Description { get; set; }

        public string TypeName { get; set; }

        public string MemberName { get; set; }

        public string MemberKind { get; set; }

        public bool Available { get; set; }

        public string Evidence { get; set; }

        public IDictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object>
            {
                { "id", Id },
                { "category", Category },
                { "description", Description },
                { "type", TypeName },
                { "member", MemberName },
                { "member_kind", MemberKind },
                { "available", Available },
                { "evidence", Evidence },
                { "evidence_level", "declaration" },
            };
        }
    }

    public sealed class CapabilitySnapshot
    {
        public SolidWorksVersion Version { get; set; }

        public string InteropVersion { get; set; }

        public string DetectedAtUtc { get; set; }

        public int ProcessId { get; set; }

        public IList<CapabilityResult> Capabilities { get; set; }

        public IDictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object>
            {
                { "schema_version", BridgeConstants.SchemaVersion },
                { "detected_at_utc", DetectedAtUtc },
                { "process_id", ProcessId },
                { "solidworks_revision", Version.Revision },
                { "solidworks_major_revision", Version.MajorRevision },
                { "solidworks_service_pack", Version.ServicePack },
                { "solidworks_release_year", Version.ReleaseYear },
                { "release_year_is_derived", true },
                { "interop_version", InteropVersion },
                { "probe_method", "interop-reflection" },
                { "capabilities", Capabilities.Select(item => item.ToDictionary()).ToArray() },
            };
        }
    }

    public sealed class CapabilityMatrixDetector
    {
        private sealed class Probe
        {
            public string Id;
            public string Category;
            public string Description;
            public string TypeName;
            public string MemberName;
            public string MemberKind;
        }

        private static readonly Probe[] Probes =
        {
            ProbeFor("addin.callback", "addin", "Register the in-process add-in callback.", "ISldWorks", "SetAddinCallbackInfo2", "method"),
            ProbeFor("taskpane.embedded", "addin", "Host the assistant inside a SolidWorks Task Pane.", "ISldWorks", "CreateTaskpaneView3", "method"),
            ProbeFor("events.active_document", "events", "Observe active-document changes.", "DSldWorksEvents_Event", "ActiveDocChangeNotify", "event"),
            ProbeFor("events.file_open", "events", "Observe completed file-open operations.", "DSldWorksEvents_Event", "FileOpenPostNotify", "event"),
            ProbeFor("events.file_new", "events", "Observe newly created documents.", "DSldWorksEvents_Event", "FileNewNotify2", "event"),
            ProbeFor("events.document_load", "events", "Observe referenced-document loads.", "DSldWorksEvents_Event", "DocumentLoadNotify2", "event"),
            ProbeFor("events.file_close", "events", "Observe file-close operations.", "DSldWorksEvents_Event", "FileCloseNotify", "event"),
            ProbeFor("events.commands", "events", "Observe command start and completion.", "DSldWorksEvents_Event", "CommandOpenPreNotify", "event"),
            ProbeFor("events.part_selection", "events", "Observe part selection changes.", "DPartDocEvents_Event", "NewSelectionNotify", "event"),
            ProbeFor("events.assembly_selection", "events", "Observe assembly selection changes.", "DAssemblyDocEvents_Event", "NewSelectionNotify", "event"),
            ProbeFor("events.drawing_selection", "events", "Observe drawing selection changes.", "DDrawingDocEvents_Event", "NewSelectionNotify", "event"),
            ProbeFor("context.selection", "selection", "Read selected objects and marks.", "ISelectionMgr", "GetSelectedObject6", "method"),
            ProbeFor("selection.persist_reference", "selection", "Create stable topology references.", "IModelDocExtension", "GetPersistReference3", "method"),
            ProbeFor("selection.restore_reference", "selection", "Restore stable topology references.", "IModelDocExtension", "GetObjectByPersistReference3", "method"),
            ProbeFor("document.save_as3", "documents", "Export through SaveAs3.", "IModelDocExtension", "SaveAs3", "method"),
            ProbeFor("assembly.pack_and_go", "assemblies", "Read and save official Pack and Go data.", "IModelDocExtension", "GetPackAndGo", "method"),
            ProbeFor("product_family.configuration", "configurations", "Create model configurations.", "IModelDoc2", "AddConfiguration3", "method"),
            ProbeFor("assembly.create_mate", "assemblies", "Create mates through structured mate data.", "IAssemblyDoc", "CreateMate", "method"),
            ProbeFor("assembly.interference", "assemblies", "Run assembly interference checks.", "IAssemblyDoc", "get_InterferenceDetectionManager", "method"),
            ProbeFor("drawing.generate", "drawings", "Create drawing views from model orientations.", "IDrawingDoc", "CreateDrawViewFromModelView3", "method"),
            ProbeFor("drawing.bom", "drawings", "Insert a BOM table on a drawing view.", "IView", "InsertBomTable6", "method"),
            ProbeFor("pmi.read", "manufacturing", "Access DimXpert data.", "IModelDocExtension", "get_DimXpertManager", "method"),
            ProbeFor("sketch.insert", "modeling", "Enter or exit a sketch through SketchManager.", "ISketchManager", "InsertSketch", "method"),
            ProbeFor("feature.extrusion3", "modeling", "Create extrusion features.", "IFeatureManager", "FeatureExtrusion3", "method"),
            ProbeFor("feature.revolve2", "modeling", "Create revolve features.", "IFeatureManager", "FeatureRevolve2", "method"),
        };

        public CapabilitySnapshot Detect(SldWorks application)
        {
            if (application == null)
            {
                throw new ArgumentNullException("application");
            }

            return Detect(application.RevisionNumber());
        }

        public CapabilitySnapshot Detect(string revision)
        {
            Assembly assembly = typeof(ISldWorks).Assembly;
            IList<CapabilityResult> capabilities = Probes.Select(probe => Evaluate(assembly, probe)).ToList();
            return new CapabilitySnapshot
            {
                Version = SolidWorksVersion.Parse(revision),
                InteropVersion = assembly.GetName().Version == null ? string.Empty : assembly.GetName().Version.ToString(),
                DetectedAtUtc = DateTime.UtcNow.ToString("o"),
                ProcessId = System.Diagnostics.Process.GetCurrentProcess().Id,
                Capabilities = capabilities,
            };
        }

        public static void WriteSnapshot(string path, CapabilitySnapshot snapshot)
        {
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            string json = serializer.Serialize(snapshot.ToDictionary());
            string temporary = path + ".tmp";
            File.WriteAllText(temporary, json, new UTF8Encoding(false));
            if (File.Exists(path))
            {
                File.Delete(path);
            }

            File.Move(temporary, path);
        }

        private static Probe ProbeFor(
            string id,
            string category,
            string description,
            string typeName,
            string memberName,
            string memberKind)
        {
            return new Probe
            {
                Id = id,
                Category = category,
                Description = description,
                TypeName = typeName,
                MemberName = memberName,
                MemberKind = memberKind,
            };
        }

        private static CapabilityResult Evaluate(Assembly assembly, Probe probe)
        {
            Type type = assembly.GetType("SolidWorks.Interop.sldworks." + probe.TypeName, false);
            bool available = false;
            if (type != null)
            {
                if (probe.MemberKind == "event")
                {
                    available = type.GetEvent(probe.MemberName) != null;
                }
                else
                {
                    available = type.GetMethods().Any(method => method.Name == probe.MemberName);
                }
            }

            return new CapabilityResult
            {
                Id = probe.Id,
                Category = probe.Category,
                Description = probe.Description,
                TypeName = probe.TypeName,
                MemberName = probe.MemberName,
                MemberKind = probe.MemberKind,
                Available = available,
                Evidence = available ? "interop-reflection" : "member-not-found",
            };
        }
    }
}
