using System;
using System.Collections.Generic;
using System.Diagnostics;

namespace SolidWorksAssistant.EventBridge
{
    public sealed class BridgeEvent
    {
        public BridgeEvent(long sequence, string sessionId, string name, IDictionary<string, object> payload)
        {
            Sequence = sequence;
            SessionId = sessionId;
            Name = name;
            Payload = payload ?? new Dictionary<string, object>();
            EventId = Guid.NewGuid().ToString("D");
            TimestampUtc = DateTime.UtcNow.ToString("o");
            ProcessId = Process.GetCurrentProcess().Id;
        }

        public int SchemaVersion { get { return BridgeConstants.SchemaVersion; } }

        public long Sequence { get; private set; }

        public string SessionId { get; private set; }

        public string EventId { get; private set; }

        public string TimestampUtc { get; private set; }

        public int ProcessId { get; private set; }

        public string Source { get { return "solidworks-addin"; } }

        public string Name { get; private set; }

        public IDictionary<string, object> Payload { get; private set; }

        public IDictionary<string, object> ToDictionary()
        {
            return new Dictionary<string, object>
            {
                { "schema_version", SchemaVersion },
                { "sequence", Sequence },
                { "session_id", SessionId },
                { "event_id", EventId },
                { "timestamp_utc", TimestampUtc },
                { "process_id", ProcessId },
                { "source", Source },
                { "name", Name },
                { "payload", Payload },
            };
        }
    }
}
