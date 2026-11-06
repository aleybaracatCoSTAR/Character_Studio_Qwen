using System;
using UnityEngine;
using Stopwatch = System.Diagnostics.Stopwatch;

namespace CoSTAR.RobinDemo
{
    /// <summary>
    /// Mono PCM16 playback. Configure/BeginTurn/Clear run on Unity's main thread;
    /// packet and completion methods may also be called from a transport thread.
    /// Events are raised by Update, never by Unity's audio thread.
    /// </summary>
    [DisallowMultipleComponent]
    [RequireComponent(typeof(AudioSource))]
    public sealed class RobinAudioPlayer : MonoBehaviour
    {
        public event Action<int, int> Played;
        public event Action<int> PlaybackFinished;
        public event Action<string> Problem;

        public bool IsPlaying => _buffer != null && _buffer.HasPendingAudio;
        // Approximate first nonzero callback output plus DSP buffering. This is
        // not a hardware loopback measurement of sound reaching the listener.
        public double FirstSoundRealtime { get; private set; } = double.NaN;

        private AudioSource _source;
        private AudioClip _clip;
        private volatile RobinPcmPlaybackBuffer _buffer;
        private int _rate = 44100;
        private int _generation;
        private bool _started;

        private static double Clock => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;

        private void Awake()
        {
            _source = GetComponent<AudioSource>();
            _source.playOnAwake = false;
            _source.loop = true;
            _source.spatialBlend = 0f;
            _source.pitch = 1f;
            Configure(_rate);
        }

        public void Configure(int sampleRate)
        {
            Clear();
            if (sampleRate != 24000 && sampleRate != 44100)
            {
                _buffer = null;
                Problem?.Invoke("Unsupported output sample rate. Expected 24000 or 44100 Hz.");
                return;
            }
            _rate = sampleRate;
            AudioSettings.GetDSPBufferSize(out int length, out int count);
            int outputRate = AudioSettings.outputSampleRate;
            // One extra DSP block is conservative: acknowledge after the last
            // sample has left both our queue and Unity's configured output ring.
            double latency = Math.Max(.02, length * (double)(Math.Max(1, count) + 1) / Math.Max(1, outputRate));
            _buffer = new RobinPcmPlaybackBuffer(sampleRate, sampleRate * 120, latency);
        }

        public void BeginTurn(int turn)
        {
            Clear();
            if (turn <= 0 || _buffer == null)
            {
                Problem?.Invoke("A positive turn identifier and a configured audio player are required.");
                return;
            }
            _generation = _buffer.BeginTurn(turn);
            RobinPcmPlaybackBuffer buffer = _buffer;
            int generation = _generation;
            // The captured buffer/generation prevents callbacks from an old clip
            // consuming a new turn after Stop or a sample-rate change.
            _clip = AudioClip.Create("Robin streamed speech", _rate, 1, _rate, true,
                samples => buffer.Read(samples, Clock, generation));
            _source.clip = _clip;
        }

        public void PushPacket(byte[] packet) { _buffer?.PushPacket(packet); }
        public void PhraseDone(int sequence) { _buffer?.PhraseDone(sequence); }
        public void ResponseDone() { _buffer?.ResponseDone(); }

        public void Clear()
        {
            _buffer?.Clear();
            _started = false;
            FirstSoundRealtime = double.NaN;
            if (_source != null) { _source.Stop(); _source.clip = null; }
            if (_clip != null) { Destroy(_clip); _clip = null; }
        }

        private void Update()
        {
            RobinPcmPlaybackBuffer buffer = _buffer;
            if (buffer == null) return;
            if (buffer.TryTakeProblem(out string problem)) { Clear(); Problem?.Invoke(problem); return; }
            if (buffer.HasPendingAudio && (AudioListener.pause || AudioListener.volume <= 0f || _source.mute || _source.volume <= 0f))
            {
                Clear(); Problem?.Invoke("Audio output was paused or muted. The reply was not acknowledged."); return;
            }
            if (!_started && buffer.ReadyToStart)
            {
                buffer.Arm(_generation);
                _source.Play();
                _started = true;
            }
            if (_started && !_source.isPlaying)
            {
                Clear(); Problem?.Invoke("Audio output stopped before the reply finished. The reply was not acknowledged."); return;
            }
            double now = Clock;
            int generation = _generation;
            if (double.IsNaN(FirstSoundRealtime) && buffer.TryFirstSound(now, out double first))
                FirstSoundRealtime = Time.realtimeSinceStartupAsDouble - Math.Max(0d, now - first);
            while (buffer.TryPlayed(now, out int turn, out int sequence))
            {
                Played?.Invoke(turn, sequence);
                // A listener may cancel or start another turn while handling ACK.
                if (_buffer != buffer || _generation != generation || buffer.Generation != generation) return;
            }
            if (buffer.TryFinished(now, out int finishedTurn))
            {
                _started = false;
                _source.Stop();
                PlaybackFinished?.Invoke(finishedTurn);
            }
        }

        private void OnApplicationPause(bool paused)
        {
            if (!paused || !IsPlaying) return;
            Clear();
            Problem?.Invoke("Audio playback was suspended. The reply was not acknowledged.");
        }

        private void OnDisable() { Clear(); }
        private void OnDestroy() { Clear(); }
    }

    /// <summary>
    /// Bounded, thread-safe queue used by the streaming clip. Read does not
    /// allocate, invoke events, or call Unity APIs. Times are monotonic seconds.
    /// </summary>
    public sealed class RobinPcmPlaybackBuffer
    {
        public const int MaximumPhrases = 256;
        public const int MaximumPacketBytes = 1024 * 1024 + 8;
        private readonly object _gate = new object();
        private readonly float[] _samples;
        private readonly Phrase[] _phrases = new Phrase[MaximumPhrases];
        private readonly int _rate;
        private readonly int _prebuffer;
        private readonly double _latency;
        private int _read, _write, _queued, _turn, _generation, _phraseCount, _nextAck;
        private long _received, _consumed;
        private bool _armed, _responseDone, _finished, _failed, _buffering;
        private double _renderEnd, _firstSound = double.NaN;
        private string _problem;

        private sealed class Phrase
        {
            public long End;
            public long Samples;
            public double Due;
            public bool Done;
            public bool Acknowledged;
        }

        public RobinPcmPlaybackBuffer(int sampleRate, int capacitySamples, double outputLatencySeconds)
        {
            if (sampleRate != 24000 && sampleRate != 44100) throw new ArgumentOutOfRangeException(nameof(sampleRate));
            if (capacitySamples < 1 || capacitySamples > sampleRate * 120) throw new ArgumentOutOfRangeException(nameof(capacitySamples));
            if (double.IsNaN(outputLatencySeconds) || double.IsInfinity(outputLatencySeconds) || outputLatencySeconds < 0 || outputLatencySeconds > 5)
                throw new ArgumentOutOfRangeException(nameof(outputLatencySeconds));
            _rate = sampleRate; _samples = new float[capacitySamples]; _latency = outputLatencySeconds;
            _prebuffer = Math.Min(capacitySamples, (int)Math.Ceiling(sampleRate * .12));
        }

        public int Generation { get { lock (_gate) return _generation; } }
        public int QueuedSamples { get { lock (_gate) return _queued; } }
        public bool HasPendingAudio { get { lock (_gate) return _turn > 0 && !_failed && !_finished && _received > 0; } }
        public bool ReadyToStart { get { lock (_gate) return !_failed && _turn > 0 && _queued > 0 && (_queued >= _prebuffer || TailClosed()); } }

        public int BeginTurn(int turn)
        {
            if (turn <= 0) throw new ArgumentOutOfRangeException(nameof(turn));
            lock (_gate) { Reset(); _turn = turn; return _generation; }
        }

        public void Clear() { lock (_gate) Reset(); }

        private void Reset()
        {
            _generation++; _turn = 0; _read = 0; _write = 0; _queued = 0; _received = 0; _consumed = 0;
            _phraseCount = 0; _nextAck = 0; _armed = false; _responseDone = false; _finished = false;
            _failed = false; _buffering = true; _renderEnd = 0; _firstSound = double.NaN; _problem = null;
            Array.Clear(_phrases, 0, _phrases.Length);
        }

        private bool Fault(string message)
        {
            if (!_failed) _problem = message;
            _failed = true; _queued = 0; _armed = false;
            return false;
        }

        private static uint BigEndian(byte[] packet, int offset)
        {
            return ((uint)packet[offset] << 24) | ((uint)packet[offset + 1] << 16) | ((uint)packet[offset + 2] << 8) | packet[offset + 3];
        }

        public bool PushPacket(byte[] packet)
        {
            lock (_gate)
            {
                if (_turn == 0 || _failed || _finished) return false;
                if (packet == null || packet.Length < 8) return Fault("The audio packet header is invalid.");
                if (BigEndian(packet, 0) != (uint)_turn) return false;
                if (packet.Length < 10 || packet.Length > MaximumPacketBytes || ((packet.Length - 8) & 1) != 0)
                    return Fault("The audio packet size is invalid.");
                uint rawSequence = BigEndian(packet, 4);
                if (rawSequence >= MaximumPhrases || rawSequence > (uint)_phraseCount || _responseDone)
                    return Fault("The audio packet sequence is invalid.");
                int sequence = (int)rawSequence;
                if (sequence == _phraseCount)
                {
                    if (sequence > 0 && !_phrases[sequence - 1].Done) return Fault("An audio phrase arrived before the previous phrase ended.");
                    _phrases[sequence] = new Phrase(); _phraseCount++;
                }
                Phrase phrase = _phrases[sequence];
                if (sequence != _phraseCount - 1 || phrase.Done) return Fault("Audio arrived after its phrase ended.");
                int count = (packet.Length - 8) / 2;
                if (count > _samples.Length - _queued) return Fault("The audio playback queue is full. The reply was not acknowledged.");
                for (int i = 8; i < packet.Length; i += 2)
                {
                    short value = unchecked((short)(packet[i] | (packet[i + 1] << 8)));
                    _samples[_write] = value / 32768f;
                    _write = (_write + 1) % _samples.Length;
                }
                _queued += count; _received += count; phrase.Samples += count; phrase.End = _received;
                return true;
            }
        }

        public bool PhraseDone(int sequence)
        {
            lock (_gate)
            {
                if (_turn == 0 || _failed || _finished) return false;
                if (sequence < 0 || sequence >= _phraseCount) return Fault("The completed audio phrase has no received audio.");
                _phrases[sequence].Done = true;
                return true;
            }
        }

        public bool ResponseDone()
        {
            lock (_gate)
            {
                if (_turn == 0 || _failed || _finished) return false;
                if (_received == 0) return Fault("The reply contained no playable audio.");
                _responseDone = true;
                return true;
            }
        }

        private bool TailClosed()
        {
            if (_responseDone) return true;
            for (int i = _nextAck; i < _phraseCount; i++)
                if (_phrases[i].Done && _phrases[i].End > _consumed) return true;
            return false;
        }

        public void Arm(int generation) { lock (_gate) { if (generation == _generation && !_failed && _turn > 0) _armed = true; } }

        public void Read(float[] output, double now, int generation)
        {
            Array.Clear(output, 0, output.Length);
            lock (_gate)
            {
                if (generation != _generation || !_armed || _failed || _turn == 0 || _finished) return;
                double start = Math.Max(now, _renderEnd);
                _renderEnd = start + output.Length / (double)_rate;
                if (_buffering && _queued < _prebuffer && !TailClosed()) return;
                int count = Math.Min(output.Length, _queued);
                long before = _consumed;
                for (int i = 0; i < count; i++)
                {
                    float value = _samples[_read]; output[i] = value;
                    _read = (_read + 1) % _samples.Length;
                    if (double.IsNaN(_firstSound) && value != 0f) _firstSound = start + i / (double)_rate + _latency;
                }
                _queued -= count; _consumed += count; _buffering = _queued == 0;
                for (int i = _nextAck; i < _phraseCount; i++)
                {
                    Phrase phrase = _phrases[i];
                    if (phrase.End > before && phrase.End <= _consumed)
                        phrase.Due = start + (phrase.End - before) / (double)_rate + _latency;
                }
            }
        }

        public bool TryFirstSound(double now, out double first)
        {
            lock (_gate) { first = _firstSound; return !_failed && _turn > 0 && !double.IsNaN(first) && now >= first; }
        }

        public bool TryPlayed(double now, out int turn, out int sequence)
        {
            lock (_gate)
            {
                turn = _turn; sequence = _nextAck;
                if (_failed || _turn == 0 || _nextAck >= _phraseCount) return false;
                Phrase phrase = _phrases[_nextAck];
                if (!phrase.Done || phrase.Samples == 0 || phrase.End > _consumed || phrase.Due <= 0 || now < phrase.Due) return false;
                phrase.Acknowledged = true; _nextAck++;
                return true;
            }
        }

        public bool TryFinished(double now, out int turn)
        {
            lock (_gate)
            {
                turn = _turn;
                if (_failed || _turn == 0 || _finished || !_responseDone || _queued > 0 || _phraseCount == 0 || _nextAck != _phraseCount) return false;
                if (now < _phrases[_phraseCount - 1].Due) return false;
                _finished = true;
                return true;
            }
        }

        public bool TryTakeProblem(out string problem)
        {
            lock (_gate) { problem = _problem; _problem = null; return problem != null; }
        }
    }
}
