using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;

namespace SolidWorksAssistant.EventBridge
{
    public sealed class DocumentEventHost : IDisposable
    {
        private readonly SldWorks application;
        private readonly Action<string, IDictionary<string, object>> publish;
        private readonly Action<IDictionary<string, object>> contextChanged;
        private readonly Dictionary<long, IDocumentSubscription> subscriptions =
            new Dictionary<long, IDocumentSubscription>();
        private bool disposed;

        public DocumentEventHost(
            SldWorks applicationValue,
            Action<string, IDictionary<string, object>> publishValue,
            Action<IDictionary<string, object>> contextChangedValue)
        {
            application = applicationValue ?? throw new ArgumentNullException(nameof(applicationValue));
            publish = publishValue ?? throw new ArgumentNullException(nameof(publishValue));
            contextChanged = contextChangedValue ?? throw new ArgumentNullException(nameof(contextChangedValue));
        }

        public int Count { get { return subscriptions.Count; } }

        public void AttachOpenDocuments()
        {
            if (disposed)
            {
                return;
            }
            try
            {
                object value = application.GetDocuments();
                Array documents = value as Array;
                if (documents != null)
                {
                    foreach (object item in documents)
                    {
                        Attach(item as ModelDoc2);
                    }
                }
                Attach(application.IActiveDoc2);
            }
            catch (COMException)
            {
                Attach(application.IActiveDoc2);
            }
        }

        public bool Attach(ModelDoc2 document)
        {
            if (disposed || document == null)
            {
                return false;
            }
            long identity = ComIdentity(document);
            if (subscriptions.ContainsKey(identity))
            {
                return false;
            }

            IDocumentSubscription subscription;
            switch (document.GetType())
            {
                case 1:
                    subscription = new PartDocumentSubscription(document, PublishDocumentEvent, OnDestroyed);
                    break;
                case 2:
                    subscription = new AssemblyDocumentSubscription(document, PublishDocumentEvent, OnDestroyed);
                    break;
                case 3:
                    subscription = new DrawingDocumentSubscription(document, PublishDocumentEvent, OnDestroyed);
                    break;
                default:
                    return false;
            }
            subscriptions.Add(identity, subscription);
            PublishDocumentEvent("document.events_attached", document, new Dictionary<string, object>
            {
                { "subscription_count", subscriptions.Count },
            });
            return true;
        }

        public void DetachByPath(string path)
        {
            string normalized = path ?? string.Empty;
            List<long> matches = new List<long>();
            foreach (KeyValuePair<long, IDocumentSubscription> pair in subscriptions)
            {
                if (string.Equals(pair.Value.Path, normalized, StringComparison.OrdinalIgnoreCase))
                {
                    matches.Add(pair.Key);
                }
            }
            foreach (long key in matches)
            {
                IDocumentSubscription subscription = subscriptions[key];
                subscriptions.Remove(key);
                subscription.Dispose();
            }
        }

        public IDictionary<string, object> ReadCurrentContext()
        {
            ModelDoc2 document = null;
            try
            {
                document = application.IActiveDoc2;
            }
            catch (COMException)
            {
                document = null;
            }
            return DocumentContextReader.Read(
                application,
                document,
                SafeRevision(),
                SafeCommandInProgress());
        }

        public void PublishCurrentContext(string reason)
        {
            IDictionary<string, object> context = ReadCurrentContext();
            publish("context.changed", new Dictionary<string, object>
            {
                { "reason", reason ?? string.Empty },
                { "context", context },
            });
            contextChanged(context);
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }
            disposed = true;
            foreach (IDocumentSubscription subscription in subscriptions.Values)
            {
                subscription.Dispose();
            }
            subscriptions.Clear();
        }

        private void PublishDocumentEvent(
            string name,
            ModelDoc2 document,
            IDictionary<string, object> details)
        {
            Dictionary<string, object> payload = new Dictionary<string, object>
            {
                { "document", DocumentContextReader.ReadDocument(document) },
                { "context", DocumentContextReader.Read(
                    application, document, SafeRevision(), SafeCommandInProgress()) },
            };
            if (details != null)
            {
                foreach (KeyValuePair<string, object> pair in details)
                {
                    payload[pair.Key] = pair.Value;
                }
            }
            publish(name, payload);
            contextChanged((IDictionary<string, object>)payload["context"]);
        }

        private void OnDestroyed(IDocumentSubscription subscription)
        {
            long key = subscription.Identity;
            subscriptions.Remove(key);
        }

        private string SafeRevision()
        {
            try
            {
                return application.RevisionNumber() ?? string.Empty;
            }
            catch (COMException)
            {
                return string.Empty;
            }
        }

        private bool SafeCommandInProgress()
        {
            try
            {
                return application.CommandInProgress;
            }
            catch (COMException)
            {
                return false;
            }
        }

        internal static long ComIdentity(object value)
        {
            IntPtr unknown = IntPtr.Zero;
            try
            {
                unknown = Marshal.GetIUnknownForObject(value);
                return unknown.ToInt64();
            }
            finally
            {
                if (unknown != IntPtr.Zero)
                {
                    Marshal.Release(unknown);
                }
            }
        }
    }

    internal interface IDocumentSubscription : IDisposable
    {
        long Identity { get; }
        string Path { get; }
    }

    internal abstract class DocumentSubscriptionBase : IDocumentSubscription
    {
        protected readonly ModelDoc2 Document;
        private readonly Action<string, ModelDoc2, IDictionary<string, object>> publish;
        private readonly Action<IDocumentSubscription> destroyed;

        protected DocumentSubscriptionBase(
            ModelDoc2 document,
            Action<string, ModelDoc2, IDictionary<string, object>> publishValue,
            Action<IDocumentSubscription> destroyedValue)
        {
            Document = document ?? throw new ArgumentNullException(nameof(document));
            publish = publishValue ?? throw new ArgumentNullException(nameof(publishValue));
            destroyed = destroyedValue ?? throw new ArgumentNullException(nameof(destroyedValue));
            Identity = DocumentEventHost.ComIdentity(document);
            try
            {
                Path = document.GetPathName() ?? string.Empty;
            }
            catch (COMException)
            {
                Path = string.Empty;
            }
        }

        public long Identity { get; private set; }
        public string Path { get; private set; }

        public abstract void Dispose();

        protected int SelectionChanged(string reason)
        {
            Publish("document.selection_changed", reason);
            return 0;
        }

        protected int Regenerated()
        {
            Publish("document.regenerated", "regen_post");
            return 0;
        }

        protected int Saved(int saveType, string fileName)
        {
            publish("document.saved", Document, new Dictionary<string, object>
            {
                { "save_type", saveType },
                { "path", fileName ?? string.Empty },
                { "reason", "file_save_post" },
            });
            return 0;
        }

        protected int Destroyed(int destroyType)
        {
            publish("document.destroyed", Document, new Dictionary<string, object>
            {
                { "destroy_type", destroyType },
                { "reason", "destroy" },
            });
            destroyed(this);
            return 0;
        }

        private void Publish(string name, string reason)
        {
            publish(name, Document, new Dictionary<string, object>
            {
                { "reason", reason },
            });
        }
    }

    internal sealed class PartDocumentSubscription : DocumentSubscriptionBase
    {
        private readonly DPartDocEvents_Event events;

        public PartDocumentSubscription(
            ModelDoc2 document,
            Action<string, ModelDoc2, IDictionary<string, object>> publish,
            Action<IDocumentSubscription> destroyed)
            : base(document, publish, destroyed)
        {
            events = document as DPartDocEvents_Event;
            if (events == null)
            {
                throw new InvalidCastException("零件文档不支持 DPartDocEvents。 ");
            }
            events.NewSelectionNotify += OnNewSelection;
            events.ClearSelectionsNotify += OnClearSelections;
            events.RegenPostNotify += OnRegenPost;
            events.FileSavePostNotify += OnFileSavePost;
            events.DestroyNotify2 += OnDestroy;
        }

        public override void Dispose()
        {
            try
            {
                events.NewSelectionNotify -= OnNewSelection;
                events.ClearSelectionsNotify -= OnClearSelections;
                events.RegenPostNotify -= OnRegenPost;
                events.FileSavePostNotify -= OnFileSavePost;
                events.DestroyNotify2 -= OnDestroy;
            }
            catch (COMException)
            {
            }
        }

        private int OnNewSelection() { return SelectionChanged("new_selection"); }
        private int OnClearSelections() { return SelectionChanged("clear_selection"); }
        private int OnRegenPost() { return Regenerated(); }
        private int OnFileSavePost(int saveType, string fileName) { return Saved(saveType, fileName); }
        private int OnDestroy(int destroyType) { return Destroyed(destroyType); }
    }

    internal sealed class AssemblyDocumentSubscription : DocumentSubscriptionBase
    {
        private readonly DAssemblyDocEvents_Event events;

        public AssemblyDocumentSubscription(
            ModelDoc2 document,
            Action<string, ModelDoc2, IDictionary<string, object>> publish,
            Action<IDocumentSubscription> destroyed)
            : base(document, publish, destroyed)
        {
            events = document as DAssemblyDocEvents_Event;
            if (events == null)
            {
                throw new InvalidCastException("装配体文档不支持 DAssemblyDocEvents。 ");
            }
            events.NewSelectionNotify += OnNewSelection;
            events.ClearSelectionsNotify += OnClearSelections;
            events.RegenPostNotify += OnRegenPost;
            events.FileSavePostNotify += OnFileSavePost;
            events.DestroyNotify2 += OnDestroy;
        }

        public override void Dispose()
        {
            try
            {
                events.NewSelectionNotify -= OnNewSelection;
                events.ClearSelectionsNotify -= OnClearSelections;
                events.RegenPostNotify -= OnRegenPost;
                events.FileSavePostNotify -= OnFileSavePost;
                events.DestroyNotify2 -= OnDestroy;
            }
            catch (COMException)
            {
            }
        }

        private int OnNewSelection() { return SelectionChanged("new_selection"); }
        private int OnClearSelections() { return SelectionChanged("clear_selection"); }
        private int OnRegenPost() { return Regenerated(); }
        private int OnFileSavePost(int saveType, string fileName) { return Saved(saveType, fileName); }
        private int OnDestroy(int destroyType) { return Destroyed(destroyType); }
    }

    internal sealed class DrawingDocumentSubscription : DocumentSubscriptionBase
    {
        private readonly DDrawingDocEvents_Event events;

        public DrawingDocumentSubscription(
            ModelDoc2 document,
            Action<string, ModelDoc2, IDictionary<string, object>> publish,
            Action<IDocumentSubscription> destroyed)
            : base(document, publish, destroyed)
        {
            events = document as DDrawingDocEvents_Event;
            if (events == null)
            {
                throw new InvalidCastException("工程图文档不支持 DDrawingDocEvents。 ");
            }
            events.NewSelectionNotify += OnNewSelection;
            events.ClearSelectionsNotify += OnClearSelections;
            events.RegenPostNotify += OnRegenPost;
            events.FileSavePostNotify += OnFileSavePost;
            events.DestroyNotify2 += OnDestroy;
        }

        public override void Dispose()
        {
            try
            {
                events.NewSelectionNotify -= OnNewSelection;
                events.ClearSelectionsNotify -= OnClearSelections;
                events.RegenPostNotify -= OnRegenPost;
                events.FileSavePostNotify -= OnFileSavePost;
                events.DestroyNotify2 -= OnDestroy;
            }
            catch (COMException)
            {
            }
        }

        private int OnNewSelection() { return SelectionChanged("new_selection"); }
        private int OnClearSelections() { return SelectionChanged("clear_selection"); }
        private int OnRegenPost() { return Regenerated(); }
        private int OnFileSavePost(int saveType, string fileName) { return Saved(saveType, fileName); }
        private int OnDestroy(int destroyType) { return Destroyed(destroyType); }
    }
}
