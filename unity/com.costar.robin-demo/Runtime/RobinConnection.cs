using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace CoSTAR.RobinDemo
{
    /// <summary>
    /// Desktop client for the existing Character Studio WebSocket. Call Pump
    /// from Update: network continuations never invoke Unity-facing events.
    /// Audio events retain the protocol's eight-byte turn/phrase header.
    /// </summary>
    public sealed class RobinConnection : IDisposable
    {
        private const int MaximumMessageBytes = 512 * 1024;
        private const int MaximumStartBytes = 3 * 1024 * 1024;
        private const int MaximumQueuedBytes = 16 * 1024 * 1024;
        private const int MaximumQueuedEvents = 2048;
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private readonly SemaphoreSlim lifecycle = new SemaphoreSlim(1, 1);
        private readonly SemaphoreSlim sending = new SemaphoreSlim(1, 1);
        private readonly ConcurrentQueue<Dispatch> pending = new ConcurrentQueue<Dispatch>();
        private ClientWebSocket socket;
        private CancellationTokenSource lifetime;
        private Task receiving;
        private int generation;
        private int disposed;
        private int queuedBytes;
        private int queuedEvents;
        private int problemGeneration;

        private sealed class Dispatch
        {
            public int Generation;
            public int Bytes;
            public JObject Json;
            public byte[] Audio;
            public string Problem;
        }

        public string BaseUrl { get; set; } = "http://127.0.0.1:18091";
        public bool Connected
        {
            get
            {
                ClientWebSocket current = socket;
                CancellationTokenSource cancellation = lifetime;
                return Volatile.Read(ref disposed) == 0 && current != null && current.State == WebSocketState.Open &&
                    cancellation != null && !cancellation.IsCancellationRequested;
            }
        }
        public event Action<JObject> Message;
        public event Action<byte[]> Audio;
        public event Action<string> Problem;

        public async Task ConnectAsync(JObject start)
        {
            ThrowIfDisposed();
            if (start == null || (string)start["type"] != "start")
                throw new ArgumentException("Supply a Character Studio start message.", nameof(start));
            byte[] bytes = Encode(start, MaximumStartBytes);
            Uri address = Address(BaseUrl, out string origin);
            int attempt = Interlocked.Increment(ref generation);
            CancelCurrent();
            await lifecycle.WaitAsync().ConfigureAwait(false);
            ClientWebSocket opened = null;
            CancellationTokenSource cancellation = null;
            try
            {
                ThrowIfDisposed();
                if (attempt != Volatile.Read(ref generation)) throw new OperationCanceledException();
                await CloseOwnedAsync().ConfigureAwait(false);
                ThrowIfDisposed();
                if (attempt != Volatile.Read(ref generation)) throw new OperationCanceledException();
                ClearPending();
                opened = new ClientWebSocket();
                opened.Options.SetRequestHeader("Origin", origin);
                opened.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
                cancellation = new CancellationTokenSource();
                socket = opened;
                lifetime = cancellation;
                if (attempt != Volatile.Read(ref generation) || Volatile.Read(ref disposed) != 0)
                    throw new OperationCanceledException();
                using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation.Token))
                {
                    deadline.CancelAfter(TimeSpan.FromSeconds(15));
                    await opened.ConnectAsync(address, deadline.Token).ConfigureAwait(false);
                }
                if (attempt != Volatile.Read(ref generation)) throw new OperationCanceledException();
                receiving = ReceiveAsync(opened, cancellation, attempt);
                await SendOwnedAsync(opened, cancellation.Token, attempt, bytes, WebSocketMessageType.Text).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                await CloseOwnedAsync().ConfigureAwait(false);
                if (attempt == Volatile.Read(ref generation) && Volatile.Read(ref disposed) == 0)
                {
                    EnqueueProblem(attempt, "The connection timed out. Check the server and SSH tunnel, then reconnect.");
                    throw new InvalidOperationException("The Character Studio connection timed out.");
                }
                throw;
            }
            catch (Exception)
            {
                await CloseOwnedAsync().ConfigureAwait(false);
                EnqueueProblem(attempt, "Could not connect to Character Studio. Check its server address, SSH tunnel and model readiness.");
                throw new InvalidOperationException("Could not connect to Character Studio.");
            }
            finally
            {
                lifecycle.Release();
            }
        }

        public Task SendAsync(JObject command)
        {
            ThrowIfDisposed();
            if (command == null) throw new ArgumentNullException(nameof(command));
            return SendCurrentAsync(Encode(command, MaximumMessageBytes), WebSocketMessageType.Text);
        }

        public Task SendAudioAsync(byte[] pcm)
        {
            ThrowIfDisposed();
            if (pcm == null || pcm.Length == 0 || pcm.Length > 8192 || pcm.Length % 2 != 0)
                throw new ArgumentException("Send a complete mono PCM16 microphone packet of at most 8192 bytes.", nameof(pcm));
            // Capture owns one packet per emission. Also protect other callers
            // that reuse their source array while a prior send is awaiting I/O.
            return SendCurrentAsync((byte[])pcm.Clone(), WebSocketMessageType.Binary);
        }

        private async Task SendCurrentAsync(byte[] bytes, WebSocketMessageType kind)
        {
            ClientWebSocket current = socket;
            CancellationTokenSource cancellation = lifetime;
            int attempt = Volatile.Read(ref generation);
            if (current == null || cancellation == null || current.State != WebSocketState.Open)
                throw new InvalidOperationException("Connect to Character Studio before sending.");
            try
            {
                await SendOwnedAsync(current, cancellation.Token, attempt, bytes, kind).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception)
            {
                if (ReferenceEquals(current, socket))
                {
                    CancelCurrent();
                    Abort(current);
                }
                EnqueueProblem(attempt, "The connection could not send data. End the call and reconnect.");
                throw new InvalidOperationException("The Character Studio connection could not send data.");
            }
        }

        private async Task SendOwnedAsync(ClientWebSocket current, CancellationToken token, int attempt,
            byte[] bytes, WebSocketMessageType kind)
        {
            await sending.WaitAsync(token).ConfigureAwait(false);
            try
            {
                if (attempt != Volatile.Read(ref generation) || !ReferenceEquals(current, socket))
                    throw new OperationCanceledException();
                await current.SendAsync(new ArraySegment<byte>(bytes), kind, true, token).ConfigureAwait(false);
            }
            finally { sending.Release(); }
        }

        private async Task ReceiveAsync(ClientWebSocket current, CancellationTokenSource cancellation, int attempt)
        {
            byte[] buffer = new byte[8192];
            try
            {
                while (!cancellation.IsCancellationRequested && attempt == Volatile.Read(ref generation))
                {
                    using (var message = new MemoryStream())
                    {
                        WebSocketMessageType? kind = null;
                        WebSocketReceiveResult result;
                        do
                        {
                            result = await current.ReceiveAsync(new ArraySegment<byte>(buffer), cancellation.Token).ConfigureAwait(false);
                            if (result.MessageType == WebSocketMessageType.Close)
                            {
                                if (!cancellation.IsCancellationRequested)
                                    EnqueueProblem(attempt, "The server closed the call. Check model readiness and reconnect.");
                                return;
                            }
                            if (kind.HasValue && kind.Value != result.MessageType)
                                throw new InvalidDataException();
                            kind = result.MessageType;
                            if (message.Length + result.Count > MaximumMessageBytes)
                                throw new InvalidDataException();
                            message.Write(buffer, 0, result.Count);
                        } while (!result.EndOfMessage);

                        byte[] complete = message.ToArray();
                        if (kind == WebSocketMessageType.Text)
                            Enqueue(new Dispatch { Generation = attempt, Bytes = complete.Length,
                                Json = JObject.Parse(StrictUtf8.GetString(complete)) });
                        else if (kind == WebSocketMessageType.Binary)
                        {
                            if (complete.Length < 10 || (complete.Length - 8) % 2 != 0)
                                throw new InvalidDataException();
                            Enqueue(new Dispatch { Generation = attempt, Bytes = complete.Length, Audio = complete });
                        }
                        else throw new InvalidDataException();
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception)
            {
                if (!cancellation.IsCancellationRequested)
                    EnqueueProblem(attempt, "The call connection ended or returned invalid data. End the call and reconnect.");
            }
            finally
            {
                try { cancellation.Cancel(); } catch (ObjectDisposedException) { }
                Abort(current);
                Interlocked.CompareExchange(ref socket, null, current);
            }
        }

        private void Enqueue(Dispatch value)
        {
            if (value.Generation != Volatile.Read(ref generation) || Volatile.Read(ref disposed) != 0) return;
            int count = Interlocked.Increment(ref queuedEvents);
            int size = Interlocked.Add(ref queuedBytes, value.Bytes);
            if (count > MaximumQueuedEvents || size > MaximumQueuedBytes)
            {
                Interlocked.Decrement(ref queuedEvents);
                Interlocked.Add(ref queuedBytes, -value.Bytes);
                throw new InvalidDataException();
            }
            pending.Enqueue(value);
        }

        private void EnqueueProblem(int attempt, string message)
        {
            if (attempt != Volatile.Read(ref generation) || Volatile.Read(ref disposed) != 0) return;
            if (Interlocked.Exchange(ref problemGeneration, attempt) == attempt) return;
            // Preserve prior JSON (especially a server startup error) when
            // there is room. A full queue must still surface one safe warning.
            if (Volatile.Read(ref queuedEvents) >= MaximumQueuedEvents ||
                Volatile.Read(ref queuedBytes) > MaximumQueuedBytes - 2048) ClearPending();
            Enqueue(new Dispatch { Generation = attempt, Bytes = message.Length * 2, Problem = message });
        }

        public void Pump()
        {
            int remaining = MaximumQueuedEvents;
            while (remaining-- > 0 && pending.TryDequeue(out Dispatch value))
            {
                Interlocked.Decrement(ref queuedEvents);
                Interlocked.Add(ref queuedBytes, -value.Bytes);
                if (value.Generation != Volatile.Read(ref generation) || Volatile.Read(ref disposed) != 0) continue;
                if (value.Json != null) Message?.Invoke(value.Json);
                else if (value.Audio != null) Audio?.Invoke(value.Audio);
                else if (value.Problem != null) Problem?.Invoke(value.Problem);
            }
        }

        public async Task DisconnectAsync()
        {
            int attempt = Interlocked.Increment(ref generation);
            CancelCurrent();
            await lifecycle.WaitAsync().ConfigureAwait(false);
            try
            {
                if (attempt == Volatile.Read(ref generation))
                {
                    await CloseOwnedAsync().ConfigureAwait(false);
                    ClearPending();
                }
            }
            finally { lifecycle.Release(); }
        }

        private async Task CloseOwnedAsync()
        {
            CancellationTokenSource cancellation = lifetime;
            ClientWebSocket current = socket;
            Task reader = receiving;
            lifetime = null; socket = null; receiving = null;
            try { cancellation?.Cancel(); } catch (ObjectDisposedException) { }
            Abort(current);
            if (reader != null)
            {
                try { await reader.ConfigureAwait(false); } catch (Exception) { }
            }
            cancellation?.Dispose();
        }

        private void CancelCurrent()
        {
            try { lifetime?.Cancel(); } catch (ObjectDisposedException) { }
        }

        private static void Abort(ClientWebSocket current)
        {
            if (current == null) return;
            try { current.Abort(); } catch (ObjectDisposedException) { }
            current.Dispose();
        }

        private void ClearPending()
        {
            while (pending.TryDequeue(out Dispatch value))
            {
                Interlocked.Decrement(ref queuedEvents);
                Interlocked.Add(ref queuedBytes, -value.Bytes);
            }
        }

        private static byte[] Encode(JObject value, int maximum)
        {
            byte[] bytes = StrictUtf8.GetBytes(value.ToString(Formatting.None));
            if (bytes.Length > maximum) throw new ArgumentException("The Character Studio message is too large.");
            return bytes;
        }

        private static Uri Address(string value, out string origin)
        {
            if (!Uri.TryCreate(value, UriKind.Absolute, out Uri source) ||
                (source.Scheme != "http" && source.Scheme != "https" && source.Scheme != "ws" && source.Scheme != "wss") ||
                !string.IsNullOrEmpty(source.UserInfo) || !string.IsNullOrEmpty(source.Query) || !string.IsNullOrEmpty(source.Fragment))
                throw new ArgumentException("Enter the Character Studio HTTP server address without credentials or query parameters.");
            var builder = new UriBuilder(source) { Scheme = source.Scheme == "https" || source.Scheme == "wss" ? "https" : "http", Path = "/", Query = "", Fragment = "" };
            origin = builder.Uri.GetLeftPart(UriPartial.Authority);
            builder.Scheme = builder.Scheme == "https" ? "wss" : "ws";
            builder.Path = "/api/live";
            return builder.Uri;
        }

        private void ThrowIfDisposed()
        {
            if (Volatile.Read(ref disposed) != 0) throw new ObjectDisposedException(nameof(RobinConnection));
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref disposed, 1) != 0) return;
            Interlocked.Increment(ref generation);
            CancelCurrent();
            ClientWebSocket current = Interlocked.Exchange(ref socket, null);
            Abort(current);
            CancellationTokenSource cancellation = Interlocked.Exchange(ref lifetime, null);
            Task reader = receiving;
            receiving = null;
            if (reader == null) cancellation?.Dispose();
            else _ = reader.ContinueWith(_ => cancellation?.Dispose(), TaskScheduler.Default);
            ClearPending();
            // An in-flight receive owns its final cancellation/disposal. Do not
            // synchronously wait on network work during Unity's OnDestroy.
        }
    }
}
