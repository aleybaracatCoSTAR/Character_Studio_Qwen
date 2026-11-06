using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace CoSTAR.RobinDemo
{
    /// <summary>
    /// Bounded desktop HTTP for the explicit server/tunnel and vetted museum
    /// images. This does not change Unity's global insecure-HTTP policy.
    /// HTTPS source validation belongs to the gallery controller. At most
    /// three redirects are followed, with the same public-host checks.
    /// </summary>
    public sealed class RobinHttp : IDisposable
    {
        private const int MaximumJsonBytes = 2 * 1024 * 1024;
        private const int MaximumAllowedBytes = 32 * 1024 * 1024;
        private static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);
        private readonly HttpClient client;
        private readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        private readonly SemaphoreSlim requests = new SemaphoreSlim(3, 3);
        private int disposed;

        public RobinHttp()
        {
            var handler = new HttpClientHandler
            {
                AllowAutoRedirect = false,
                UseCookies = false,
                UseDefaultCredentials = false
            };
            // Keep the platform's normal certificate and proxy validation.
            // A linked token bounds both waiting for a slot and streaming.
            client = new HttpClient(handler, true) { Timeout = Timeout.InfiniteTimeSpan };
        }

        public async Task<byte[]> GetBytesAsync(string url, int maxBytes, CancellationToken token = default)
        {
            if (Volatile.Read(ref disposed) != 0) throw new ObjectDisposedException(nameof(RobinHttp));
            if (maxBytes < 1 || maxBytes > MaximumAllowedBytes)
                throw new ArgumentException("Choose a download limit between one byte and 32 MiB.", nameof(maxBytes));
            Uri address = ValidateAddress(url);
            Uri initial = address;
            bool loopback = IsLoopback(address);
            bool acquired = false;
            CancellationTokenSource deadline;
            try { deadline = CancellationTokenSource.CreateLinkedTokenSource(token, lifetime.Token); }
            catch (ObjectDisposedException) { throw new ObjectDisposedException(nameof(RobinHttp)); }
            using (deadline)
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(18));
                try
                {
                    await requests.WaitAsync(deadline.Token).ConfigureAwait(false);
                    acquired = true;
                    if (Volatile.Read(ref disposed) != 0) throw new OperationCanceledException(deadline.Token);
                    for (int redirects = 0; redirects <= 3; redirects++)
                    {
                        if (!loopback) await ValidatePublicDnsAsync(address, deadline.Token).ConfigureAwait(false);
                        using (HttpResponseMessage response = await client.GetAsync(address,
                            HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false))
                        {
                            int status = (int)response.StatusCode;
                            if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308)
                            {
                                if (redirects == 3 || response.Headers.Location == null) throw new InvalidDataException();
                                Uri next;
                                try { next = ValidateAddress(new Uri(address, response.Headers.Location).AbsoluteUri); }
                                catch (ArgumentException) { throw new InvalidDataException(); }
                                catch (UriFormatException) { throw new InvalidDataException(); }
                                if (loopback)
                                {
                                    if (next.Scheme != initial.Scheme || next.Host != initial.Host || next.Port != initial.Port)
                                        throw new InvalidDataException();
                                }
                                else if (!IsPublicHost(next)) throw new InvalidDataException();
                                address = next;
                                continue;
                            }
                            if (!response.IsSuccessStatusCode) throw new InvalidDataException();
                            long? length = response.Content.Headers.ContentLength;
                            if (length.HasValue && (length.Value < 0 || length.Value > maxBytes))
                                throw new InvalidDataException();
                            // Disposing the individual response also bounds
                            // native implementations whose ReadAsync cannot
                            // interrupt an already pending socket read.
                            using (deadline.Token.Register(() => response.Dispose()))
                            using (Stream input = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                            using (var output = new MemoryStream(Math.Min(8192, maxBytes)))
                            {
                                byte[] buffer = new byte[8192];
                                while (true)
                                {
                                    int count = await input.ReadAsync(buffer, 0, buffer.Length, deadline.Token).ConfigureAwait(false);
                                    if (count == 0) break;
                                    if (output.Length + count > maxBytes) throw new InvalidDataException();
                                    output.Write(buffer, 0, count);
                                }
                                deadline.Token.ThrowIfCancellationRequested();
                                if (length.HasValue && output.Length != length.Value) throw new InvalidDataException();
                                return output.ToArray();
                            }
                        }
                    }
                    throw new InvalidDataException();
                }
                catch (OperationCanceledException)
                {
                    if (token.IsCancellationRequested || Volatile.Read(ref disposed) != 0)
                        throw new OperationCanceledException("The request was cancelled.", token);
                    throw new TimeoutException("The request timed out. Check the server and SSH tunnel, then try again.");
                }
                catch (InvalidDataException)
                {
                    throw new InvalidDataException("The resource was unavailable, too large, incomplete, or redirected outside its allowed source.");
                }
                catch (Exception)
                {
                    if (token.IsCancellationRequested || Volatile.Read(ref disposed) != 0)
                        throw new OperationCanceledException("The request was cancelled.", token);
                    if (deadline.IsCancellationRequested) throw new TimeoutException("The request timed out. Check the server and SSH tunnel, then try again.");
                    throw new InvalidOperationException("The resource could not be downloaded. Check the server or museum source and try again.");
                }
                finally { if (acquired) requests.Release(); }
            }
        }

        public async Task<JToken> GetJsonAsync(string url, CancellationToken token = default)
        {
            byte[] bytes = await GetBytesAsync(url, MaximumJsonBytes, token).ConfigureAwait(false);
            token.ThrowIfCancellationRequested();
            if (Volatile.Read(ref disposed) != 0) throw new OperationCanceledException();
            try
            {
                string json = StrictUtf8.GetString(bytes);
                if (json.Length > 0 && json[0] == '\uFEFF') json = json.Substring(1);
                using (var text = new StringReader(json))
                using (var reader = new JsonTextReader(text) { MaxDepth = 64, DateParseHandling = DateParseHandling.None })
                {
                    JToken value = JToken.ReadFrom(reader);
                    if (reader.Read()) throw new JsonReaderException();
                    return value;
                }
            }
            catch (Exception)
            {
                throw new InvalidDataException("The server did not return readable JSON data.");
            }
        }

        private static Uri ValidateAddress(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value.Length > 8192 ||
                !Uri.TryCreate(value, UriKind.Absolute, out Uri address) ||
                !string.IsNullOrEmpty(address.UserInfo) || !string.IsNullOrEmpty(address.Fragment) ||
                (address.Scheme != "http" && address.Scheme != "https"))
                throw new ArgumentException("Use an HTTP loopback server or an HTTPS resource URL without credentials.");
            if (address.Scheme == "http" && !IsLoopback(address))
                throw new ArgumentException("Unencrypted HTTP is limited to the explicit localhost SSH tunnel.");
            if (!IsLoopback(address) && !IsPublicHost(address))
                throw new ArgumentException("Use a public HTTPS museum source.");
            return address;
        }

        private static bool IsLoopback(Uri address)
        {
            bool loopback = string.Equals(address.Host, "localhost", StringComparison.OrdinalIgnoreCase);
            if (IPAddress.TryParse(address.Host.Trim('[', ']'), out IPAddress ip))
            {
                if (ip.IsIPv4MappedToIPv6) ip = ip.MapToIPv4();
                loopback = IPAddress.IsLoopback(ip);
            }
            return loopback;
        }

        private static bool IsPublicHost(Uri address)
        {
            if (address.Scheme != "https" || !address.IsDefaultPort || address.HostNameType != UriHostNameType.Dns || IsLoopback(address)) return false;
            string host = address.DnsSafeHost.TrimEnd('.').ToLowerInvariant();
            if (!host.Contains(".")) return false;
            foreach (string suffix in new[] { ".local", ".lan", ".internal", ".home", ".localhost", ".onion" })
                if (host.EndsWith(suffix, StringComparison.Ordinal)) return false;
            return true;
        }

        private static async Task ValidatePublicDnsAsync(Uri address, CancellationToken token)
        {
            if (!IsPublicHost(address)) throw new InvalidDataException();
            Task<IPAddress[]> lookup = Dns.GetHostAddressesAsync(address.DnsSafeHost);
            var cancelled = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            using (token.Register(() => cancelled.TrySetResult(true)))
            {
                if (await Task.WhenAny(lookup, cancelled.Task).ConfigureAwait(false) != lookup)
                {
                    _ = lookup.ContinueWith(task => { var ignored = task.Exception; }, TaskContinuationOptions.OnlyOnFaulted);
                    throw new OperationCanceledException(token);
                }
                IPAddress[] addresses = await lookup.ConfigureAwait(false);
                if (addresses.Length == 0) throw new InvalidDataException();
                foreach (IPAddress ip in addresses) if (!IsPublicAddress(ip)) throw new InvalidDataException();
            }
        }

        private static bool IsPublicAddress(IPAddress address)
        {
            if (address.IsIPv4MappedToIPv6) address = address.MapToIPv4();
            byte[] b = address.GetAddressBytes();
            if (b.Length == 4)
            {
                if (b[0] == 0 || b[0] == 10 || b[0] == 127 || b[0] >= 224 ||
                    b[0] == 100 && b[1] >= 64 && b[1] <= 127 ||
                    b[0] == 169 && b[1] == 254 || b[0] == 172 && b[1] >= 16 && b[1] <= 31 ||
                    b[0] == 192 && (b[1] == 168 || b[1] == 0 && (b[2] == 0 || b[2] == 2)) ||
                    b[0] == 198 && (b[1] == 18 || b[1] == 19 || b[1] == 51 && b[2] == 100) ||
                    b[0] == 203 && b[1] == 0 && b[2] == 113) return false;
                return true;
            }
            // Public global-unicast IPv6; exclude the documentation prefix.
            return b.Length == 16 && b[0] >= 0x20 && b[0] <= 0x3f &&
                !(b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8);
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref disposed, 1) != 0) return;
            lifetime.Cancel();
            client.Dispose();
            lifetime.Dispose();
            // Waiting requests still release their semaphore in finally;
            // SemaphoreSlim has no native handle unless one is requested.
        }
    }
}
