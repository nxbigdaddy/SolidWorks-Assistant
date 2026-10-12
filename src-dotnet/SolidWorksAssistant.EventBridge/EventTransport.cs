using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace SolidWorksAssistant.EventBridge
{
    public interface IEventSink : IDisposable
    {
        void Write(BridgeEvent bridgeEvent);
    }

    public sealed class JsonLineEventSink : IEventSink
    {
        private const long MaxBytes = 10L * 1024L * 1024L;
        private readonly object sync = new object();
        private readonly string path;
        private readonly JavaScriptSerializer serializer = new JavaScriptSerializer();

        public JsonLineEventSink(string path)
        {
            this.path = path;
        }

        public void Write(BridgeEvent bridgeEvent)
        {
            string json = serializer.Serialize(bridgeEvent.ToDictionary());
            lock (sync)
            {
                RotateIfNeeded();
                using (FileStream stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                using (StreamWriter writer = new StreamWriter(stream, new UTF8Encoding(false)))
                {
                    writer.WriteLine(json);
                }
            }
        }

        public void Dispose()
        {
        }

        private void RotateIfNeeded()
        {
            FileInfo info = new FileInfo(path);
            if (!info.Exists || info.Length < MaxBytes)
            {
                return;
            }

            string previous = path + ".1";
            if (File.Exists(previous))
            {
                File.Delete(previous);
            }

            File.Move(path, previous);
        }
    }

    public sealed class NamedPipeEventSink : IEventSink
    {
        private readonly string pipeName;
        private NamedPipeClientStream stream;
        private StreamWriter writer;
        private DateTime retryAfterUtc = DateTime.MinValue;

        public NamedPipeEventSink(string pipeName)
        {
            this.pipeName = pipeName;
        }

        public void Write(BridgeEvent bridgeEvent)
        {
            if (!EnsureConnected())
            {
                return;
            }

            try
            {
                JavaScriptSerializer serializer = new JavaScriptSerializer();
                writer.WriteLine(serializer.Serialize(bridgeEvent.ToDictionary()));
                writer.Flush();
            }
            catch (IOException)
            {
                ResetConnection();
            }
        }

        public void Dispose()
        {
            ResetConnection();
        }

        private bool EnsureConnected()
        {
            if (stream != null && stream.IsConnected)
            {
                return true;
            }

            if (DateTime.UtcNow < retryAfterUtc)
            {
                return false;
            }

            ResetConnection();
            try
            {
                stream = new NamedPipeClientStream(".", pipeName, PipeDirection.Out, PipeOptions.Asynchronous);
                stream.Connect(10);
                writer = new StreamWriter(stream, new UTF8Encoding(false));
                return true;
            }
            catch (TimeoutException)
            {
                retryAfterUtc = DateTime.UtcNow.AddSeconds(2);
                ResetConnection();
                return false;
            }
            catch (IOException)
            {
                retryAfterUtc = DateTime.UtcNow.AddSeconds(2);
                ResetConnection();
                return false;
            }
        }

        private void ResetConnection()
        {
            if (writer != null)
            {
                writer.Dispose();
                writer = null;
            }

            if (stream != null)
            {
                stream.Dispose();
                stream = null;
            }
        }
    }

    public sealed class CompositeEventSink : IEventSink
    {
        private readonly IEventSink[] sinks;
        private readonly Action<Exception> onError;

        public CompositeEventSink(Action<Exception> onError, params IEventSink[] sinks)
        {
            this.sinks = sinks ?? new IEventSink[0];
            this.onError = onError;
        }

        public void Write(BridgeEvent bridgeEvent)
        {
            foreach (IEventSink sink in sinks)
            {
                try
                {
                    sink.Write(bridgeEvent);
                }
                catch (Exception exception)
                {
                    if (onError != null)
                    {
                        onError(exception);
                    }
                }
            }
        }

        public void Dispose()
        {
            foreach (IEventSink sink in sinks)
            {
                try
                {
                    sink.Dispose();
                }
                catch (Exception exception)
                {
                    if (onError != null)
                    {
                        onError(exception);
                    }
                }
            }
        }
    }

    public sealed class EventDispatcher : IDisposable
    {
        private readonly Queue<BridgeEvent> queue = new Queue<BridgeEvent>();
        private readonly object sync = new object();
        private readonly int normalCapacity;
        private readonly IEventSink sink;
        private readonly Thread worker;
        private readonly string sessionId = Guid.NewGuid().ToString("D");
        private long sequence;
        private long droppedEvents;
        private bool disposed;

        public EventDispatcher(IEventSink sink, int normalCapacity = 512)
        {
            if (sink == null)
            {
                throw new ArgumentNullException("sink");
            }

            this.sink = sink;
            this.normalCapacity = Math.Max(1, normalCapacity);
            worker = new Thread(DispatchLoop);
            worker.Name = "SolidWorksAssistant.EventBridge.Dispatcher";
            worker.IsBackground = true;
            worker.Start();
        }

        public string SessionId { get { return sessionId; } }

        public long DroppedEvents { get { return Interlocked.Read(ref droppedEvents); } }

        public bool Publish(string name, IDictionary<string, object> payload)
        {
            if (string.IsNullOrWhiteSpace(name))
            {
                return false;
            }

            BridgeEvent bridgeEvent = new BridgeEvent(
                Interlocked.Increment(ref sequence), sessionId, name, payload);
            lock (sync)
            {
                if (disposed)
                {
                    return false;
                }

                if (queue.Count >= normalCapacity && !IsCritical(name))
                {
                    Interlocked.Increment(ref droppedEvents);
                    return false;
                }

                // Critical lifecycle and command-boundary events may exceed the normal
                // backlog limit. They remain FIFO ordered and are never displaced by
                // high-frequency context or selection notifications.
                queue.Enqueue(bridgeEvent);
                Monitor.Pulse(sync);
                return true;
            }
        }

        public void Dispose()
        {
            lock (sync)
            {
                if (disposed)
                {
                    return;
                }

                disposed = true;
                Monitor.PulseAll(sync);
            }

            if (!worker.Join(TimeSpan.FromSeconds(5)))
            {
                // Closing the transport can unblock a stalled named-pipe write. Never
                // dispose queue state while the worker is still using it.
                sink.Dispose();
                worker.Join(TimeSpan.FromSeconds(1));
                return;
            }

            sink.Dispose();
        }

        private void DispatchLoop()
        {
            while (true)
            {
                BridgeEvent bridgeEvent;
                lock (sync)
                {
                    while (queue.Count == 0 && !disposed)
                    {
                        Monitor.Wait(sync);
                    }

                    if (queue.Count == 0 && disposed)
                    {
                        return;
                    }

                    bridgeEvent = queue.Dequeue();
                }

                sink.Write(bridgeEvent);
            }
        }

        private static bool IsCritical(string name)
        {
            return name.StartsWith("command.", StringComparison.Ordinal)
                || name.StartsWith("bridge.", StringComparison.Ordinal)
                || name.StartsWith("solidworks.", StringComparison.Ordinal);
        }
    }
}
