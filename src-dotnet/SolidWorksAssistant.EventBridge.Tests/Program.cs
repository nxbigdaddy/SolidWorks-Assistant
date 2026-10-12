using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using SolidWorks.Interop.swpublished;

namespace SolidWorksAssistant.EventBridge.Tests
{
    internal static class Program
    {
        private static int failures;

        private static int Main()
        {
            Run("version parser", TestVersionParser);
            Run("capability reflection", TestCapabilityReflection);
            Run("event dispatcher", TestEventDispatcher);
            Run("event dispatcher priority", TestEventDispatcherPriority);
            Run("jsonl transport", TestJsonLineTransport);
            Run("addin contract", TestAddinContract);
            Run("context contract", TestContextContract);
            Run("task pane contract", TestTaskPaneContract);

            Console.WriteLine(failures == 0 ? "ALL TESTS PASSED" : failures + " TEST(S) FAILED");
            return failures == 0 ? 0 : 1;
        }

        private static void TestVersionParser()
        {
            SolidWorksVersion version = SolidWorksVersion.Parse("33.5.0.53");
            Assert(version.MajorRevision == 33, "major revision");
            Assert(version.ServicePack == 5, "service pack");
            Assert(version.ReleaseYear == 2025, "release year");
        }

        private static void TestCapabilityReflection()
        {
            CapabilitySnapshot snapshot = new CapabilityMatrixDetector().Detect("33.5.0.53");
            Assert(snapshot.Capabilities.Count >= 20, "capability count");
            Assert(snapshot.Capabilities.All(item => item.Available), "all local interop capabilities available");
            Assert(snapshot.InteropVersion.StartsWith("33.5", StringComparison.Ordinal), "interop version");
        }

        private static void TestEventDispatcher()
        {
            RecordingSink sink = new RecordingSink();
            using (EventDispatcher dispatcher = new EventDispatcher(sink))
            {
                Assert(dispatcher.Publish("test.one", new Dictionary<string, object>()), "first event accepted");
                Assert(dispatcher.Publish("test.two", new Dictionary<string, object>()), "second event accepted");
            }

            Assert(sink.Events.Count == 2, "events drained on dispose");
            Assert(sink.Events[0].Sequence == 1 && sink.Events[1].Sequence == 2, "monotonic sequence");
            Assert(sink.Events[0].SessionId == sink.Events[1].SessionId, "stable session id");
        }

        private static void TestEventDispatcherPriority()
        {
            BlockingSink sink = new BlockingSink();
            using (EventDispatcher dispatcher = new EventDispatcher(sink, 2))
            {
                Assert(dispatcher.Publish("context.changed", new Dictionary<string, object>()), "blocking event accepted");
                Assert(sink.Started.WaitOne(TimeSpan.FromSeconds(1)), "worker entered sink");
                Assert(dispatcher.Publish("selection.changed", new Dictionary<string, object>()), "queued normal event accepted");
                Assert(dispatcher.Publish("document.changed", new Dictionary<string, object>()), "second normal event accepted");
                Assert(!dispatcher.Publish("context.changed", new Dictionary<string, object>()), "overflow event rejected explicitly");
                Assert(dispatcher.Publish("command.started", new Dictionary<string, object>()), "critical event accepted beyond normal backlog");
                Assert(dispatcher.DroppedEvents == 1, "drop counter recorded");
                sink.Release.Set();
            }

            Assert(sink.Events.Any(item => item.Name == "command.started"), "critical command event drained");
            Assert(sink.Events.Select(item => item.Sequence).SequenceEqual(
                sink.Events.Select(item => item.Sequence).OrderBy(item => item)), "delivered sequence remains ordered");
        }

        private static void TestJsonLineTransport()
        {
            string directory = Path.Combine(Path.GetTempPath(), "sw-event-bridge-test-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            string path = Path.Combine(directory, "events.jsonl");
            try
            {
                using (JsonLineEventSink sink = new JsonLineEventSink(path))
                {
                    sink.Write(new BridgeEvent(1, "session", "document.opened", new Dictionary<string, object>
                    {
                        { "title", "测试零件" },
                    }));
                }

                string line = File.ReadAllLines(path)[0];
                Dictionary<string, object> value = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line);
                Assert(Convert.ToInt32(value["schema_version"]) == 1, "schema version");
                Assert((string)value["name"] == "document.opened", "event name");
                Assert(line.Contains("测试零件"), "utf-8 payload");
            }
            finally
            {
                Directory.Delete(directory, true);
            }
        }

        private static void TestAddinContract()
        {
            Type type = typeof(SolidWorksAddin);
            Assert(typeof(ISwAddin).IsAssignableFrom(type), "ISwAddin implemented");
            Assert(type.GUID.ToString().Equals(BridgeConstants.AddinGuid, StringComparison.OrdinalIgnoreCase), "stable COM guid");
            Assert(Environment.Is64BitProcess, "x64 test process");
            SolidWorksAddin addin = new SolidWorksAddin();
            IntPtr dispatch = Marshal.GetIDispatchForObject(addin);
            try
            {
                Assert(dispatch != IntPtr.Zero, "IDispatch callback available");
            }
            finally
            {
                if (dispatch != IntPtr.Zero)
                {
                    Marshal.Release(dispatch);
                }
            }
        }

        private static void TestContextContract()
        {
            IDictionary<string, object> context = DocumentContextReader.Read(
                null,
                null,
                "33.5.0",
                false);
            Assert((string)context["solidworks_revision"] == "33.5.0", "context revision");
            Assert(((IDictionary<string, object>)context["document"]).Count == 0, "empty document");
            Assert(DocumentContextReader.SelectionTypeName(2) == "face", "selection type mapping");
            Assert(Convert.ToInt32(context["stale_after_ms"]) == 15000, "context freshness");
        }

        private static void TestTaskPaneContract()
        {
            Type type = typeof(AssistantTaskPaneControl);
            Assert(type.BaseType != null && type.BaseType.Name == "UserControl", "WinForms host control");
            Assert(typeof(IDisposable).IsAssignableFrom(typeof(AssistantTaskPaneHost)), "task pane disposable");
        }

        private static void Run(string name, Action test)
        {
            try
            {
                test();
                Console.WriteLine("PASS " + name);
            }
            catch (Exception exception)
            {
                failures++;
                Console.Error.WriteLine("FAIL " + name + ": " + exception.Message);
            }
        }

        private static void Assert(bool condition, string message)
        {
            if (!condition)
            {
                throw new InvalidOperationException(message);
            }
        }

        private sealed class RecordingSink : IEventSink
        {
            public IList<BridgeEvent> Events { get; private set; } = new List<BridgeEvent>();

            public void Write(BridgeEvent bridgeEvent)
            {
                Events.Add(bridgeEvent);
            }

            public void Dispose()
            {
            }
        }

        private sealed class BlockingSink : IEventSink
        {
            public readonly ManualResetEvent Started = new ManualResetEvent(false);
            public readonly ManualResetEvent Release = new ManualResetEvent(false);
            public IList<BridgeEvent> Events { get; private set; } = new List<BridgeEvent>();

            public void Write(BridgeEvent bridgeEvent)
            {
                Started.Set();
                Release.WaitOne(TimeSpan.FromSeconds(2));
                Events.Add(bridgeEvent);
            }

            public void Dispose()
            {
                Release.Set();
                Started.Dispose();
                Release.Dispose();
            }
        }
    }
}
