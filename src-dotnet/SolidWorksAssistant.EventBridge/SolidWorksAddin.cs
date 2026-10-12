using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swpublished;

namespace SolidWorksAssistant.EventBridge
{
    [ComVisible(true)]
    [Guid(BridgeConstants.AddinGuid)]
    [ProgId("SolidWorksAssistant.EventBridge")]
    [ClassInterface(ClassInterfaceType.AutoDispatch)]
    public sealed class SolidWorksAddin : ISwAddin
    {
        private SldWorks application;
        private EventDispatcher dispatcher;
        private BridgePaths paths;
        private DocumentEventHost documentEvents;
        private AssistantTaskPaneHost taskPane;
        private DateTime lastHeartbeatUtc = DateTime.MinValue;
        private string revision = string.Empty;
        private int cookie;

        public bool ConnectToSW(object thisSW, int cookieValue)
        {
            try
            {
                application = thisSW as SldWorks;
                if (application == null)
                {
                    return false;
                }

                cookie = cookieValue;
                paths = BridgePaths.Default();
                paths.Ensure();
                IEventSink sink = new CompositeEventSink(
                    WriteDiagnostic,
                    new JsonLineEventSink(paths.Events),
                    new NamedPipeEventSink(BridgeConstants.PipeName));
                dispatcher = new EventDispatcher(sink);

                if (!application.SetAddinCallbackInfo2(0, this, cookie))
                {
                    Publish("bridge.callback_registration_failed", EmptyPayload());
                    DisposeBridge();
                    return false;
                }

                AttachEvents();
                CapabilitySnapshot snapshot = new CapabilityMatrixDetector().Detect(application);
                revision = snapshot.Version.Revision ?? string.Empty;
                CapabilityMatrixDetector.WriteSnapshot(paths.Capabilities, snapshot);
                documentEvents = new DocumentEventHost(application, Publish, UpdateTaskPane);
                documentEvents.AttachOpenDocuments();
                bool taskPaneAvailable = TryCreateTaskPane();
                documentEvents.PublishCurrentContext("bridge_connected");
                Publish("bridge.connected", new Dictionary<string, object>
                {
                    { "revision", snapshot.Version.Revision },
                    { "release_year", snapshot.Version.ReleaseYear },
                    { "interop_version", snapshot.InteropVersion },
                    { "cookie", cookie },
                    { "capability_count", snapshot.Capabilities.Count },
                    { "document_subscription_count", documentEvents.Count },
                    { "taskpane_available", taskPaneAvailable },
                    { "active_document", ReadActiveDocument() },
                });
                return true;
            }
            catch (Exception exception)
            {
                WriteDiagnostic(exception);
                DisposeBridge();
                return false;
            }
        }

        public bool DisconnectFromSW()
        {
            try
            {
                Publish("bridge.disconnecting", new Dictionary<string, object>
                {
                    { "cookie", cookie },
                });
                DetachEvents();
                DisposeBridge();
                return true;
            }
            catch (Exception exception)
            {
                WriteDiagnostic(exception);
                DisposeBridge();
                return false;
            }
        }

        [ComRegisterFunction]
        public static void RegisterFunction(Type type)
        {
            Registration.Register(type);
        }

        [ComUnregisterFunction]
        public static void UnregisterFunction(Type type)
        {
            Registration.Unregister(type);
        }

        private void AttachEvents()
        {
            application.ActiveDocChangeNotify += OnActiveDocChange;
            application.FileOpenPostNotify += OnFileOpenPost;
            application.FileNewNotify2 += OnFileNew;
            application.DocumentLoadNotify2 += OnDocumentLoad;
            application.FileCloseNotify += OnFileClose;
            application.CommandOpenPreNotify += OnCommandOpen;
            application.CommandCloseNotify += OnCommandClose;
            application.OnIdleNotify += OnIdle;
            application.DestroyNotify += OnDestroy;
        }

        private void DetachEvents()
        {
            if (application == null)
            {
                return;
            }

            try
            {
                application.ActiveDocChangeNotify -= OnActiveDocChange;
                application.FileOpenPostNotify -= OnFileOpenPost;
                application.FileNewNotify2 -= OnFileNew;
                application.DocumentLoadNotify2 -= OnDocumentLoad;
                application.FileCloseNotify -= OnFileClose;
                application.CommandOpenPreNotify -= OnCommandOpen;
                application.CommandCloseNotify -= OnCommandClose;
                application.OnIdleNotify -= OnIdle;
                application.DestroyNotify -= OnDestroy;
            }
            catch (COMException exception)
            {
                WriteDiagnostic(exception);
            }
        }

        private int OnActiveDocChange()
        {
            if (documentEvents != null)
            {
                documentEvents.Attach(application.IActiveDoc2);
            }
            Publish("document.active_changed", new Dictionary<string, object>
            {
                { "document", ReadActiveDocument() },
            });
            PublishCurrentContext("active_document_changed");
            return 0;
        }

        private int OnFileOpenPost(string fileName)
        {
            if (documentEvents != null)
            {
                documentEvents.Attach(application.IActiveDoc2);
            }
            Publish("document.opened", new Dictionary<string, object>
            {
                { "path", fileName ?? string.Empty },
                { "document", ReadActiveDocument() },
            });
            PublishCurrentContext("document_opened");
            return 0;
        }

        private int OnFileNew(object newDocument, int documentType, string templateName)
        {
            ModelDoc2 document = newDocument as ModelDoc2;
            if (documentEvents != null)
            {
                documentEvents.Attach(document);
            }
            Publish("document.created", new Dictionary<string, object>
            {
                { "document_type", documentType },
                { "template", templateName ?? string.Empty },
                { "document", ReadDocument(document) },
            });
            PublishCurrentContext("document_created");
            return 0;
        }

        private int OnDocumentLoad(string documentTitle, string documentPath)
        {
            Publish("document.loaded", new Dictionary<string, object>
            {
                { "title", documentTitle ?? string.Empty },
                { "path", documentPath ?? string.Empty },
            });
            return 0;
        }

        private int OnFileClose(string fileName, int reason)
        {
            Publish("document.closed", new Dictionary<string, object>
            {
                { "path", fileName ?? string.Empty },
                { "reason", reason },
            });
            if (documentEvents != null)
            {
                documentEvents.DetachByPath(fileName);
            }
            PublishCurrentContext("document_closed");
            return 0;
        }

        private int OnCommandOpen(int command, int userCommand)
        {
            Publish("command.started", new Dictionary<string, object>
            {
                { "command", command },
                { "user_command", userCommand },
                { "document", ReadActiveDocument() },
                { "context", ReadCurrentContext() },
            });
            return 0;
        }

        private int OnCommandClose(int command, int reason)
        {
            Publish("command.finished", new Dictionary<string, object>
            {
                { "command", command },
                { "reason", reason },
                { "document", ReadActiveDocument() },
                { "context", ReadCurrentContext() },
            });
            PublishCurrentContext("command_finished");
            return 0;
        }

        private int OnIdle()
        {
            DateTime now = DateTime.UtcNow;
            if ((now - lastHeartbeatUtc).TotalSeconds >= 10)
            {
                lastHeartbeatUtc = now;
                Publish("bridge.heartbeat", new Dictionary<string, object>
                {
                    { "command_in_progress", SafeCommandInProgress() },
                    { "document", ReadActiveDocument() },
                    { "context", ReadCurrentContext() },
                });
                UpdateTaskPane(ReadCurrentContext());
            }

            return 0;
        }

        private int OnDestroy()
        {
            Publish("solidworks.destroying", EmptyPayload());
            return 0;
        }

        private IDictionary<string, object> ReadActiveDocument()
        {
            if (application == null)
            {
                return EmptyPayload();
            }

            try
            {
                return ReadDocument(application.IActiveDoc2);
            }
            catch (COMException exception)
            {
                WriteDiagnostic(exception);
                return new Dictionary<string, object> { { "read_error", exception.ErrorCode } };
            }
        }

        private static IDictionary<string, object> ReadDocument(ModelDoc2 document)
        {
            return DocumentContextReader.ReadDocument(document);
        }

        private bool SafeCommandInProgress()
        {
            try
            {
                return application != null && application.CommandInProgress;
            }
            catch (COMException)
            {
                return false;
            }
        }

        private void Publish(string name, IDictionary<string, object> payload)
        {
            EventDispatcher current = dispatcher;
            if (current != null && !current.Publish(name, payload))
            {
                long dropped = current.DroppedEvents;
                if (dropped == 1 || dropped % 100 == 0)
                {
                    WriteDiagnostic(new InvalidOperationException(
                        "Event bridge backlog dropped " + dropped + " non-critical event(s)."));
                }
            }
        }

        private void DisposeBridge()
        {
            AssistantTaskPaneHost currentTaskPane = taskPane;
            taskPane = null;
            if (currentTaskPane != null)
            {
                currentTaskPane.Dispose();
            }

            DocumentEventHost currentDocumentEvents = documentEvents;
            documentEvents = null;
            if (currentDocumentEvents != null)
            {
                currentDocumentEvents.Dispose();
            }

            EventDispatcher current = dispatcher;
            dispatcher = null;
            if (current != null)
            {
                current.Dispose();
            }

            application = null;
            revision = string.Empty;
            cookie = 0;
        }

        private bool TryCreateTaskPane()
        {
            try
            {
                taskPane = new AssistantTaskPaneHost(application);
                taskPane.SetConnected(true, revision);
                Publish("taskpane.created", new Dictionary<string, object>
                {
                    { "title", "AI 智能体（SolidWorks 助手）" },
                });
                return true;
            }
            catch (Exception exception)
            {
                WriteDiagnostic(exception);
                Publish("taskpane.failed", new Dictionary<string, object>
                {
                    { "error", exception.Message },
                });
                taskPane = null;
                return false;
            }
        }

        private IDictionary<string, object> ReadCurrentContext()
        {
            if (documentEvents != null)
            {
                return documentEvents.ReadCurrentContext();
            }
            return DocumentContextReader.Read(
                application,
                application == null ? null : application.IActiveDoc2,
                revision,
                SafeCommandInProgress());
        }

        private void PublishCurrentContext(string reason)
        {
            if (documentEvents != null)
            {
                documentEvents.PublishCurrentContext(reason);
            }
        }

        private void UpdateTaskPane(IDictionary<string, object> context)
        {
            AssistantTaskPaneHost current = taskPane;
            if (current != null)
            {
                current.UpdateContext(context);
            }
        }

        private void WriteDiagnostic(Exception exception)
        {
            try
            {
                BridgePaths currentPaths = paths ?? BridgePaths.Default();
                currentPaths.Ensure();
                string line = DateTime.UtcNow.ToString("o") + " " + exception;
                File.AppendAllText(currentPaths.Diagnostics, line + System.Environment.NewLine);
            }
            catch
            {
                // Diagnostics must never interrupt SOLIDWORKS callbacks.
            }
        }

        private static IDictionary<string, object> EmptyPayload()
        {
            return new Dictionary<string, object>();
        }
    }
}
