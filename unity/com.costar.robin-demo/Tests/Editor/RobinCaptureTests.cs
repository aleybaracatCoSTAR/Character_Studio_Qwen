using System;
using System.Collections.Generic;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;

namespace CoSTAR.RobinDemo.Tests
{
    public sealed class RobinCaptureTests
    {
        [TestCase(16000)]
        [TestCase(44100)]
        [TestCase(48000)]
        [TestCase(96000)]
        [TestCase(8000)]
        public void ArbitraryInputBoundariesKeepTheSameContinuousResampling(int sourceRate)
        {
            // One extra source frame supplies interpolation's final endpoint,
            // including input devices whose rate is below the wire rate.
            int frames = sourceRate * 2 + 1;
            float[] input = new float[frames * 2];
            for (int frame = 0; frame < frames; frame++)
            {
                input[frame * 2] = (float)Math.Sin(frame * 2 * Math.PI * 300 / sourceRate) * .7f;
                input[frame * 2 + 1] = (float)Math.Sin(frame * 2 * Math.PI * 500 / sourceRate) * .3f;
            }
            List<byte[]> whole = Encode(sourceRate, 2, input, new[] { frames });
            List<byte[]> fragmented = Encode(sourceRate, 2, input, new[] { 1, 3, 17, 481, 2, 997 });
            Assert.That(whole.Count, Is.EqualTo(50));
            Assert.That(fragmented.Count, Is.EqualTo(whole.Count));
            for (int index = 0; index < whole.Count; index++)
            {
                Assert.That(whole[index].Length, Is.EqualTo(1280));
                CollectionAssert.AreEqual(whole[index], fragmented[index]);
            }
        }

        [Test]
        public void OutputIsHeaderlessMonoPcm16LittleEndianWithSafeClamping()
        {
            float[] input = new float[640 * 2];
            float[] left = { 1, -1, 1, float.NaN, float.PositiveInfinity, 2, -2, .25f };
            float[] right = { 1, -1, -1, float.NaN, 1, 2, -2, .75f };
            for (int frame = 0; frame < 640; frame++)
            {
                input[frame * 2] = left[frame % left.Length];
                input[frame * 2 + 1] = right[frame % right.Length];
            }
            List<byte[]> packets = Encode(16000, 2, input, new[] { 640 });
            Assert.That(packets.Count, Is.EqualTo(1));
            Assert.That(packets[0].Length, Is.EqualTo(640 * 2));
            short[] expected = { 32767, -32768, 0, 0, 16383, 32767, -32768, 16383 };
            for (int frame = 0; frame < 640; frame++)
                Assert.That(Sample(packets[0], frame), Is.EqualTo(expected[frame % expected.Length]));
            Assert.That(packets[0][0], Is.EqualTo(255));
            Assert.That(packets[0][1], Is.EqualTo(127));
        }

        [Test]
        public void ResetDiscardsThePartialPacketAndPreviousInterpolationSample()
        {
            var encoder = new RobinPcmEncoder(16000, 1);
            var packets = new List<byte[]>();
            encoder.Packet += packets.Add;
            encoder.Push(Constant(320, 1), 320);
            Assert.That(packets, Is.Empty);
            encoder.Reset();
            encoder.Push(Constant(640, -1), 640);
            Assert.That(packets.Count, Is.EqualTo(1));
            for (int frame = 0; frame < 640; frame++)
                Assert.That(Sample(packets[0], frame), Is.EqualTo(-32768));
        }

        [Test]
        public void EmittedPacketsAreOwnedAndNeverReusedForLaterAudio()
        {
            var encoder = new RobinPcmEncoder(16000, 1);
            var packets = new List<byte[]>();
            encoder.Packet += packets.Add;
            encoder.Push(Constant(640, 1), 640);
            byte[] first = (byte[])packets[0].Clone();
            encoder.Push(Constant(640, -1), 640);
            Assert.That(packets.Count, Is.EqualTo(2));
            Assert.That(ReferenceEquals(packets[0], packets[1]), Is.False);
            CollectionAssert.AreEqual(first, packets[0]);
        }

        [Test]
        public void ResetInsideThePacketCallbackStopsTheOldInputImmediately()
        {
            var encoder = new RobinPcmEncoder(16000, 1);
            var packets = new List<byte[]>();
            encoder.Packet += bytes => { packets.Add(bytes); encoder.Reset(); };
            encoder.Push(Constant(2000, 1), 2000);
            Assert.That(packets.Count, Is.EqualTo(1));
            encoder.Push(Constant(640, -1), 640);
            Assert.That(packets.Count, Is.EqualTo(2));
            Assert.That(Sample(packets[1], 0), Is.EqualTo(-32768));
        }

        [Test]
        public void InvalidRatesChannelsAndIncompleteBuffersAreRejected()
        {
            Assert.Throws<ArgumentException>(() => new RobinPcmEncoder(0, 1));
            Assert.Throws<ArgumentException>(() => new RobinPcmEncoder(48000, 0));
            Assert.Throws<ArgumentException>(() => new RobinPcmEncoder(48000, 17));
            var encoder = new RobinPcmEncoder(48000, 2);
            Assert.Throws<ArgumentException>(() => encoder.Push(null, 0));
            Assert.Throws<ArgumentException>(() => encoder.Push(new float[4], 3));
            Assert.Throws<ArgumentException>(() => encoder.Push(new float[4], -1));
        }

        [Test]
        public void RepeatedListeningStateDoesNotDiscardAnIncompletePacket()
        {
            // The controller applies this state every Update. No microphone is
            // started: inject only the pure encoder to check pause transitions.
            var owner = new GameObject("Capture state fixture");
            try
            {
                var capture = owner.AddComponent<RobinMicrophone>();
                var encoder = new RobinPcmEncoder(16000, 1);
                int packets = 0;
                encoder.Packet += _ => packets++;
                typeof(RobinMicrophone).GetField("encoder", BindingFlags.Instance | BindingFlags.NonPublic)
                    .SetValue(capture, encoder);
                encoder.Push(Constant(320, 1), 320);
                for (int frame = 0; frame < 10; frame++) capture.SetPaused(false);
                encoder.Push(Constant(320, 1), 320);
                Assert.That(packets, Is.EqualTo(1));
                encoder.Push(Constant(320, 1), 320);
                capture.SetPaused(true);
                capture.SetPaused(false);
                encoder.Push(Constant(320, 1), 320);
                Assert.That(packets, Is.EqualTo(1), "Resume must discard the earlier partial packet.");
            }
            finally { UnityEngine.Object.DestroyImmediate(owner); }
        }

        private static List<byte[]> Encode(int sourceRate, int channels, float[] samples, int[] chunks)
        {
            var encoder = new RobinPcmEncoder(sourceRate, channels);
            var result = new List<byte[]>();
            encoder.Packet += result.Add;
            int offset = 0, part = 0, frames = samples.Length / channels;
            while (offset < frames)
            {
                int count = Math.Min(chunks[part++ % chunks.Length], frames - offset);
                var chunk = new float[count * channels];
                Array.Copy(samples, offset * channels, chunk, 0, chunk.Length);
                encoder.Push(chunk, count);
                offset += count;
            }
            return result;
        }

        private static float[] Constant(int count, float value)
        {
            var samples = new float[count];
            for (int index = 0; index < count; index++) samples[index] = value;
            return samples;
        }

        private static short Sample(byte[] packet, int frame)
        {
            return unchecked((short)(packet[frame * 2] | packet[frame * 2 + 1] << 8));
        }
    }
}
