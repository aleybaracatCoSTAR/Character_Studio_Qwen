using System;
using NUnit.Framework;

namespace CoSTAR.RobinDemo.Tests
{
    public class RobinAudioPlayerTests
    {
        private static byte[] Packet(int turn, int sequence, params short[] values)
        {
            var packet = new byte[8 + values.Length * 2];
            for (int i = 0; i < 4; i++)
            {
                packet[i] = (byte)((uint)turn >> (24 - i * 8));
                packet[4 + i] = (byte)((uint)sequence >> (24 - i * 8));
            }
            for (int i = 0; i < values.Length; i++)
            {
                packet[8 + i * 2] = (byte)values[i];
                packet[9 + i * 2] = (byte)((ushort)values[i] >> 8);
            }
            return packet;
        }

        private static RobinPcmPlaybackBuffer Buffer(int capacity = 32, double latency = .05, int rate = 24000)
        {
            return new RobinPcmPlaybackBuffer(rate, capacity, latency);
        }

        [Test]
        public void DecodesBigEndianIdentifiersAndLittleEndianSignedPcmWithoutChangingSampleCount()
        {
            var buffer = Buffer();
            int generation = buffer.BeginTurn(0x01020304);
            Assert.That(buffer.PushPacket(Packet(0x01020304, 0, short.MinValue, -1, 0, short.MaxValue)), Is.True);
            buffer.PhraseDone(0); buffer.Arm(generation);
            var output = new float[7];
            buffer.Read(output, 10, generation);
            Assert.That(output[0], Is.EqualTo(-1f));
            Assert.That(output[1], Is.EqualTo(-1f / 32768));
            Assert.That(output[2], Is.Zero);
            Assert.That(output[3], Is.EqualTo(32767f / 32768));
            CollectionAssert.AreEqual(new float[] { 0, 0, 0 }, new[] { output[4], output[5], output[6] });
            Assert.That(buffer.QueuedSamples, Is.Zero);
        }

        [TestCase(24000)]
        [TestCase(44100)]
        public void KeepsNativeRateAndWaitsForTheLastSampleAndDspLatency(int rate)
        {
            const double latency = .08;
            var buffer = Buffer(rate: rate, latency: latency);
            int generation = buffer.BeginTurn(7);
            buffer.PushPacket(Packet(7, 0, 100, 200, 300)); buffer.PhraseDone(0); buffer.ResponseDone();
            Assert.That(buffer.TryPlayed(1000, out _, out _), Is.False, "Receipt is not playback.");
            buffer.Arm(generation); buffer.Read(new float[16], 10, generation);
            double due = 10 + 3d / rate + latency;
            Assert.That(buffer.TryPlayed(due - .000001, out _, out _), Is.False);
            Assert.That(buffer.TryFinished(due + 1, out _), Is.False, "Phrase ACK must precede response ACK.");
            Assert.That(buffer.TryPlayed(due + .000001, out int turn, out int sequence), Is.True);
            Assert.That(turn, Is.EqualTo(7)); Assert.That(sequence, Is.Zero);
            Assert.That(buffer.TryFinished(due + .000001, out turn), Is.True);
            Assert.That(turn, Is.EqualTo(7));
            Assert.That(buffer.TryFinished(due + 1, out _), Is.False, "Completion is emitted once.");
        }

        [Test]
        public void UnarmedPrefetchCannotConsumeOrAcknowledgeAnySamples()
        {
            var buffer = Buffer(); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 100, 200)); buffer.PhraseDone(0); buffer.ResponseDone();
            var output = new float[] { 9, 9, 9 };
            buffer.Read(output, 10, generation);
            CollectionAssert.AreEqual(new float[] { 0, 0, 0 }, output);
            Assert.That(buffer.QueuedSamples, Is.EqualTo(2));
            Assert.That(buffer.TryPlayed(100, out _, out _), Is.False);
        }

        [Test]
        public void FinalShortPhraseFlushesBelowPrebufferAndRetainsEveryTailSample()
        {
            var buffer = Buffer(capacity: 10000); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 10, 20, 30));
            Assert.That(buffer.ReadyToStart, Is.False);
            buffer.PhraseDone(0); Assert.That(buffer.ReadyToStart, Is.True);
            buffer.Arm(generation); var output = new float[10]; buffer.Read(output, 5, generation);
            Assert.That(output[2], Is.EqualTo(30f / 32768)); Assert.That(output[3], Is.Zero);
            Assert.That(buffer.TryPlayed(6, out _, out _), Is.True);
            Assert.That(buffer.TryFinished(6, out _), Is.False, "response_done is an independent gate.");
            buffer.ResponseDone(); Assert.That(buffer.TryFinished(6, out _), Is.True);
        }

        [Test]
        public void PhraseDoneAfterConsumptionStillWaitsForItsPreviouslyRecordedTail()
        {
            var buffer = Buffer(capacity: 4); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 1, 2, 3, 4)); buffer.Arm(generation);
            buffer.Read(new float[4], 10, generation);
            Assert.That(buffer.TryPlayed(20, out _, out _), Is.False);
            buffer.PhraseDone(0);
            Assert.That(buffer.TryPlayed(10.01, out _, out _), Is.False);
            Assert.That(buffer.TryPlayed(20, out _, out _), Is.True);
        }

        [Test]
        public void MultiplePacketsInOnePhraseAndRingWrapKeepEverySampleInOrder()
        {
            var buffer = Buffer(capacity: 8); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 1, 2, 3, 4, 5, 6, 7, 8)); buffer.Arm(generation);
            var first = new float[6]; buffer.Read(first, 10, generation);
            buffer.PushPacket(Packet(1, 0, 9, 10, 11, 12, 13, 14)); buffer.PhraseDone(0); buffer.ResponseDone();
            var second = new float[8]; buffer.Read(second, 11, generation);
            for (int i = 0; i < first.Length; i++) Assert.That(first[i], Is.EqualTo((i + 1) / 32768f));
            for (int i = 0; i < second.Length; i++) Assert.That(second[i], Is.EqualTo((i + 7) / 32768f));
            Assert.That(buffer.QueuedSamples, Is.Zero);
            Assert.That(buffer.TryPlayed(12, out _, out _), Is.True);
            Assert.That(buffer.TryPlayed(12, out _, out _), Is.False);
        }

        [Test]
        public void UnderrunPaddingNeverCountsAsReceivedAudioOrClosesThePhrase()
        {
            var buffer = Buffer(capacity: 4); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 1, 2, 3, 4)); buffer.Arm(generation);
            var first = new float[8]; buffer.Read(first, 10, generation);
            Assert.That(first[4], Is.Zero); Assert.That(buffer.HasPendingAudio, Is.True);
            Assert.That(buffer.TryPlayed(20, out _, out _), Is.False);
            buffer.PushPacket(Packet(1, 0, 5, 6)); buffer.PhraseDone(0); buffer.ResponseDone();
            var last = new float[4]; buffer.Read(last, 11, generation);
            Assert.That(last[0], Is.EqualTo(5f / 32768)); Assert.That(last[1], Is.EqualTo(6f / 32768));
            Assert.That(buffer.TryPlayed(12, out _, out _), Is.True); Assert.That(buffer.TryFinished(12, out _), Is.True);
        }

        [Test]
        public void PhrasesAcknowledgeInSequenceOnlyAfterBothPlaybackAndClosure()
        {
            var buffer = Buffer(); int generation = buffer.BeginTurn(2);
            buffer.PushPacket(Packet(2, 0, 1, 2)); buffer.PhraseDone(0);
            buffer.PushPacket(Packet(2, 1, 3)); buffer.PhraseDone(1); buffer.ResponseDone();
            buffer.Arm(generation); buffer.Read(new float[3], 10, generation);
            Assert.That(buffer.TryPlayed(11, out _, out int sequence), Is.True); Assert.That(sequence, Is.Zero);
            Assert.That(buffer.TryFinished(11, out _), Is.False);
            Assert.That(buffer.TryPlayed(11, out _, out sequence), Is.True); Assert.That(sequence, Is.EqualTo(1));
            Assert.That(buffer.TryFinished(11, out int turn), Is.True); Assert.That(turn, Is.EqualTo(2));
        }

        [Test]
        public void ClearAndNewTurnInvalidateOldCallbacksPacketsAndPendingAcknowledgements()
        {
            var buffer = Buffer(); int oldGeneration = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 100)); buffer.PhraseDone(0); buffer.ResponseDone(); buffer.Arm(oldGeneration);
            buffer.Read(new float[1], 10, oldGeneration); buffer.Clear();
            int currentGeneration = buffer.BeginTurn(2);
            buffer.PushPacket(Packet(2, 0, 200)); buffer.PhraseDone(0); buffer.ResponseDone(); buffer.Arm(currentGeneration);
            var stale = new float[1]; buffer.Read(stale, 20, oldGeneration);
            Assert.That(stale[0], Is.Zero); Assert.That(buffer.QueuedSamples, Is.EqualTo(1));
            Assert.That(buffer.PushPacket(Packet(1, 0, 300)), Is.False);
            Assert.That(buffer.TryTakeProblem(out _), Is.False);
            buffer.Read(stale, 20, currentGeneration); Assert.That(stale[0], Is.EqualTo(200f / 32768));
            Assert.That(buffer.TryPlayed(21, out int turn, out _), Is.True); Assert.That(turn, Is.EqualTo(2));
        }

        [Test]
        public void MalformedPacketOrQueueOverflowFailsClosedAndNeverAcknowledges()
        {
            byte[] odd = Packet(1, 0, 1); Array.Resize(ref odd, 9);
            byte[][] invalid = { new byte[4], odd, Packet(1, 3, 1), Packet(1, 0, 1, 2, 3, 4, 5) };
            foreach (byte[] packet in invalid)
            {
                var buffer = Buffer(capacity: 4); int generation = buffer.BeginTurn(1);
                Assert.That(buffer.PushPacket(packet), Is.False);
                Assert.That(buffer.TryTakeProblem(out string problem), Is.True); Assert.That(problem, Is.Not.Empty);
                Assert.That(buffer.TryTakeProblem(out _), Is.False, "One failure notification only.");
                buffer.Arm(generation); buffer.Read(new float[8], 10, generation);
                Assert.That(buffer.TryPlayed(100, out _, out _), Is.False); Assert.That(buffer.TryFinished(100, out _), Is.False);
            }
        }

        [Test]
        public void AudioArrivingAfterPhraseClosureInvalidatesTheReplyInsteadOfAcknowledgingPartialData()
        {
            var buffer = Buffer(); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 100)); buffer.PhraseDone(0); buffer.Arm(generation);
            buffer.Read(new float[1], 10, generation);
            Assert.That(buffer.PushPacket(Packet(1, 0, 200)), Is.False);
            Assert.That(buffer.TryPlayed(20, out _, out _), Is.False);
        }

        [Test]
        public void FirstSoundUsesCallbackPositionAndLatencyRatherThanPacketReceiptOrLeadingSilence()
        {
            var buffer = Buffer(latency: .1); int generation = buffer.BeginTurn(1);
            buffer.PushPacket(Packet(1, 0, 0, 0, 100)); buffer.PhraseDone(0);
            Assert.That(buffer.TryFirstSound(100, out _), Is.False);
            buffer.Arm(generation); buffer.Read(new float[10], 10, generation);
            Assert.That(buffer.TryFirstSound(10.05, out _), Is.False);
            Assert.That(buffer.TryFirstSound(11, out double first), Is.True);
            Assert.That(first, Is.EqualTo(10 + 2d / 24000 + .1).Within(.0000001));
            buffer.Clear(); Assert.That(buffer.TryFirstSound(100, out _), Is.False);
        }

        [Test]
        public void EmptyResponseCannotBecomeSuccessfulPlayback()
        {
            var buffer = Buffer(); buffer.BeginTurn(1);
            Assert.That(buffer.ResponseDone(), Is.False); Assert.That(buffer.TryTakeProblem(out _), Is.True);
            Assert.That(buffer.TryFinished(100, out _), Is.False);
        }
    }
}
