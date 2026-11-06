using System;
using UnityEngine;

namespace CoSTAR.RobinDemo
{
    /// <summary>
    /// Explicit desktop microphone capture. Pause discards samples instead of
    /// replaying the character's speaker echo when recording resumes.
    /// </summary>
    public sealed class RobinMicrophone : MonoBehaviour
    {
        private const int RingSeconds = 2;
        private AudioClip clip;
        private string device;
        private RobinPcmEncoder encoder;
        private float[] scratch;
        private int readPosition;
        private int readFrames;
        private bool paused;
        private bool hasSamples;
        private double started;
        private double lastPoll;

        public bool IsRecording => clip != null && Microphone.IsRecording(device);
        public event Action<byte[]> Packet;
        public event Action<string> Problem;

        public void StartCapture()
        {
            if (clip != null) return;
            try
            {
                // null selects the operating system's default input device.
                // Microphone.Start is reached only by this explicit UI action.
                device = null;
                int frequency = 48000;
                Microphone.GetDeviceCaps(device, out int minimum, out int maximum);
                if (minimum > 0) frequency = Math.Max(minimum, frequency);
                if (maximum > 0) frequency = Math.Min(maximum, frequency);
                clip = Microphone.Start(device, true, RingSeconds, frequency);
                if (clip == null) throw new InvalidOperationException();
                encoder = new RobinPcmEncoder(clip.frequency, clip.channels);
                encoder.Packet += bytes => { if (!paused) Packet?.Invoke(bytes); };
                readFrames = Math.Max(1, clip.frequency / 100); // about 10 ms of source frames
                scratch = new float[readFrames * clip.channels];
                readPosition = Math.Max(0, Microphone.GetPosition(device));
                started = lastPoll = Time.realtimeSinceStartupAsDouble;
                hasSamples = false;
            }
            catch (Exception)
            {
                StopCapture();
                Problem?.Invoke("Could not start the microphone. Check the input device and microphone permission, then try again.");
            }
        }

        public void SetPaused(bool value)
        {
            if (paused == value) return;
            paused = value;
            DiscardCaptured();
        }

        private void DiscardCaptured()
        {
            if (clip != null)
            {
                int position = Microphone.GetPosition(device);
                if (position >= 0) readPosition = position;
            }
            encoder?.Reset();
            lastPoll = Time.realtimeSinceStartupAsDouble;
        }

        private void Update()
        {
            if (clip == null || encoder == null) return;
            try { ReadAvailable(); }
            catch (Exception)
            {
                StopCapture();
                Problem?.Invoke("The microphone samples could not be read. Check the input device and start capture again.");
            }
        }

        private void ReadAvailable()
        {
            double now = Time.realtimeSinceStartupAsDouble;
            int position = Microphone.GetPosition(device);
            if (position < 0 || (hasSamples && !Microphone.IsRecording(device)) || (!hasSamples && now - started > 10))
            {
                StopCapture();
                Problem?.Invoke("The microphone is not providing audio. Check its permission and input device, then start capture again.");
                return;
            }
            if (position != 0) hasSamples = true;
            int available = (position - readPosition + clip.samples) % clip.samples;
            if (paused || now - lastPoll >= RingSeconds || available > clip.frequency / 2)
            {
                DiscardCaptured();
                return;
            }
            lastPoll = now;
            while (available >= readFrames)
            {
                if (!clip.GetData(scratch, readPosition))
                {
                    StopCapture();
                    Problem?.Invoke("The microphone samples could not be read. Start capture again.");
                    return;
                }
                readPosition = (readPosition + readFrames) % clip.samples;
                available -= readFrames;
                encoder.Push(scratch, readFrames);
                // A Packet subscriber can synchronously pause or stop capture.
                if (paused || clip == null || encoder == null) return;
            }
        }

        public void StopCapture()
        {
            AudioClip owned = clip;
            encoder?.Reset();
            clip = null; encoder = null; scratch = null; hasSamples = false;
            readPosition = 0; paused = false;
            if (owned != null)
            {
                try { Microphone.End(device); } catch (Exception) { }
                Destroy(owned);
            }
        }

        private void OnDisable() { StopCapture(); }
        private void OnDestroy() { StopCapture(); }
    }

    /// <summary>Continuous interleaved-float to mono 16 kHz PCM16LE encoder.</summary>
    public sealed class RobinPcmEncoder
    {
        public const int OutputRate = 16000;
        public const int PacketBytes = 1280; // 640 mono samples / 40 ms
        private readonly int channels;
        private readonly double step;
        private byte[] packet = new byte[PacketBytes];
        private int used;
        private long sourceFrame;
        private double nextOutput;
        private float previous;
        private bool hasPrevious;
        private int revision;
        public event Action<byte[]> Packet;

        public RobinPcmEncoder(int sourceRate, int sourceChannels)
        {
            if (sourceRate < 4000 || sourceRate > 192000 || sourceChannels < 1 || sourceChannels > 16)
                throw new ArgumentException("Use a supported microphone sample rate and channel count.");
            channels = sourceChannels;
            step = sourceRate / (double)OutputRate;
        }

        public void Reset()
        {
            revision++;
            used = 0; sourceFrame = 0; nextOutput = 0; previous = 0; hasPrevious = false;
        }

        public void Push(float[] interleaved, int frames)
        {
            if (interleaved == null || frames < 0 || frames > interleaved.Length / channels)
                throw new ArgumentException("Supply complete interleaved microphone frames.");
            int expectedRevision = revision;
            for (int frame = 0; frame < frames; frame++)
            {
                double sum = 0;
                for (int channel = 0; channel < channels; channel++)
                {
                    float value = interleaved[frame * channels + channel];
                    if (!float.IsNaN(value) && !float.IsInfinity(value))
                        sum += Math.Max(-1, Math.Min(1, value));
                }
                float current = (float)(sum / channels);
                if (!hasPrevious)
                {
                    hasPrevious = true;
                    previous = current;
                    nextOutput = step;
                    sourceFrame = 1;
                    Write(current);
                    if (expectedRevision != revision) return;
                    continue;
                }
                while (nextOutput <= sourceFrame)
                {
                    double fraction = nextOutput - (sourceFrame - 1);
                    Write((float)(previous + (current - previous) * fraction));
                    if (expectedRevision != revision) return;
                    nextOutput += step;
                }
                previous = current;
                sourceFrame++;
            }
        }

        private void Write(float value)
        {
            value = Math.Max(-1, Math.Min(1, value));
            short sample = (short)(value < 0 ? value * 32768 : value * 32767);
            packet[used++] = (byte)sample;
            packet[used++] = (byte)(sample >> 8);
            if (used != packet.Length) return;
            byte[] complete = packet;
            packet = new byte[PacketBytes];
            used = 0;
            Packet?.Invoke(complete);
        }
    }
}
