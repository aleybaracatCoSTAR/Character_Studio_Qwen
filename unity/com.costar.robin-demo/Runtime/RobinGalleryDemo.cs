using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using TMPro;
using UnityEngine;

namespace CoSTAR.RobinDemo
{
    /// <summary>A desktop Unity client. GPU models and catalogue storage stay on the existing server.</summary>
    public sealed class RobinGalleryDemo : MonoBehaviour
    {
        public RobinDemoView View;
        public RobinAudioPlayer Player;
        public RobinMicrophone Microphone;
        public TextAsset DefaultProfileText;
        public string ServerBaseUrl = "http://127.0.0.1:18091";

        readonly RobinConnection connection = new RobinConnection();
        readonly RobinHttp web = new RobinHttp();
        readonly Dictionary<string, Take> comparisons = new Dictionary<string, Take>();
        readonly List<Texture2D> textures = new List<Texture2D>();
        readonly StringBuilder answer = new StringBuilder();
        JObject profile, capabilities;
        Take take;
        bool connecting, sessionReady, replying, polling, manualEngine, initialized, backgroundCaptured, previousBackground;
        int currentTurn, sessionTurns, imageEpoch, liveEpoch, audioQueued;
        double nextPoll, resumeAt;
        string question = "", pendingQuestion = "", sourceUrl = "";
        string Engine => View.EngineDropdown.value == 1 ? "qwen-omni" : "baseline";
        static string Label(string engine) => engine == "qwen-omni" ? "Streaming Qwen3-Omni" : "Qwen + Drama 3";

        sealed class Take
        {
            public string Engine, Prompt, Reply, Settings, Sources = "";
            public int Turn;
            public bool Complete, Heard;
            public JObject Metrics = new JObject();
            public double TurnStarted, PlaybackEstimateMs;
        }

        void Start()
        {
            if (!View || !Player || !Microphone)
            {
                Debug.LogError("Robin demo references are missing. Recreate the demo scene with Tools > CoSTAR.");
                enabled = false; return;
            }
            profile = JObject.Parse((DefaultProfileText ? DefaultProfileText : Resources.Load<TextAsset>("robin-profile")).text);
            View.ServerInput.SetTextWithoutNotify(ServerBaseUrl);
            View.ConnectButton.onClick.AddListener(() => _ = ToggleConnection());
            View.TalkButton.onClick.AddListener(ToggleMicrophone);
            View.SendButton.onClick.AddListener(() => _ = SubmitText());
            View.StopButton.onClick.AddListener(() => _ = Interrupt());
            View.CompareButton.onClick.AddListener(ShowComparison);
            if (View.SourceButton) View.SourceButton.onClick.AddListener(() => { if (PublicUrl(sourceUrl)) Application.OpenURL(sourceUrl); });
            View.ServerInput.onEndEdit.AddListener(_ => { if (!connection.Connected && !connecting) { capabilities = null; nextPoll = 0; } });
            View.EngineDropdown.onValueChanged.AddListener(selectedEngine => { manualEngine = true; if (connection.Connected) _ = Disconnect(); UpdateControls(); });
            connection.Message += Message;
            connection.Audio += bytes => { try { Player.PushPacket(bytes); } catch { Fail("The audio packet could not be played. Reconnect and try again."); } };
            connection.Problem += Fail;
            Player.Problem += Fail;
            Microphone.Problem += Fail;
            Player.Played += (turn, seq) => _ = Control(new JObject { ["type"] = "played", ["turn_id"] = turn, ["seq"] = seq });
            Player.PlaybackFinished += PlaybackFinished;
            Microphone.Packet += bytes => { if (sessionReady && !replying && Time.realtimeSinceStartupAsDouble >= resumeAt) _ = Audio(bytes); };
            View.ReplyText.text = "Meet Robin, your gallery guide.\n\nConnect to your running Character Studio server, then type or turn on the microphone.";
            initialized = true;
            previousBackground = Application.runInBackground; backgroundCaptured = true;
            Application.runInBackground = true;
            UpdateControls();
            StartCoroutine(RefreshServer());
        }

#if UNITY_EDITOR
        double lastEditorTick;
        void OnEnable() { UnityEditor.EditorApplication.update += EditorTick; }
        void OnDisable() { UnityEditor.EditorApplication.update -= EditorTick; }
        void EditorTick()
        {
            if (!Application.isPlaying || UnityEditor.EditorApplication.timeSinceStartup - lastEditorTick < 1d / 60d) return;
            lastEditorTick = UnityEditor.EditorApplication.timeSinceStartup;
            UnityEditor.EditorApplication.QueuePlayerLoopUpdate();
        }
#endif

        void Update()
        {
            if (!initialized) return;
            connection.Pump();
            if (Microphone.IsRecording) Microphone.SetPaused(!sessionReady || replying || Time.realtimeSinceStartupAsDouble < resumeAt);
            if (!polling && Time.realtimeSinceStartupAsDouble >= nextPoll && !connecting) StartCoroutine(RefreshServer());
        }

        static string Text(JToken value, int limit = 16000)
        {
            var result = value?.Type == JTokenType.String ? (string)value : "";
            return result.Length > limit ? result.Substring(0, limit) : result;
        }

        string BaseUrl()
        {
            if (!Uri.TryCreate(View.ServerInput.text.Trim(), UriKind.Absolute, out var uri) ||
                (uri.Scheme != "http" && uri.Scheme != "https") || !string.IsNullOrEmpty(uri.UserInfo))
                throw new InvalidOperationException("Enter your server's http:// or https:// address.");
            return uri.GetLeftPart(UriPartial.Authority);
        }

        IEnumerator GetJson(string path, Action<JToken> success, Action failure = null)
        {
            string url;
            try { url = BaseUrl() + path; }
            catch { failure?.Invoke(); yield break; }
            Task<JToken> operation;
            try { operation = web.GetJsonAsync(url); }
            catch { failure?.Invoke(); yield break; }
            while (!operation.IsCompleted) yield return null;
            if (operation.IsCanceled || operation.IsFaulted) { var observed = operation.Exception; failure?.Invoke(); yield break; }
            success(operation.Result);
        }

        IEnumerator RefreshServer()
        {
            polling = true;
            yield return GetJson("/api/status", data =>
            {
                capabilities = (JObject)data;
                if (!manualEngine && comparisons.Count == 0 && !connection.Connected)
                    View.EngineDropdown.SetValueWithoutNotify((string)data["comparison"]?["default_engine"] == "qwen-omni" ? 1 : 0);
                if (!connection.Connected) SetStatus(data.Value<bool?>("busy") == true ? "Another call is active. End it in the browser before connecting here." : data.Value<bool?>("preview") == true ? "Design preview server — connect to your Surrey tunnel for a live call." : Ready() ? "Ready to meet Robin" : "Waiting for the selected engine");
            }, () => { capabilities = null; if (!connection.Connected) SetStatus("Server unavailable. Start your Surrey job and SSH tunnel, then connect."); });
            polling = false; nextPoll = Time.realtimeSinceStartupAsDouble + 5; UpdateControls();
        }

        bool Ready() => (capabilities?.Value<bool?>("busy") != true || connection.Connected) && capabilities?["preview"]?.Value<bool>() != true && capabilities?["comparison"]?["engines"]?[Engine]?["ready"]?.Value<bool>() == true;

        async Task ToggleConnection()
        {
            if (connection.Connected || sessionReady) { await Disconnect(); return; }
            if (connecting || !Ready()) return;
            connecting = true; int epoch = ++liveEpoch;
            SetStatus("Opening the gallery conversation…"); UpdateControls();
            // Only public catalogue settings are read here. Browser-edited characters are
            // intentionally not guessed from a name or taken from browser local storage.
            bool loaded = false;
            StartCoroutine(PrepareProfile(() => loaded = true));
            double deadline = Time.realtimeSinceStartupAsDouble + 25;
            while (!loaded && epoch == liveEpoch && Time.realtimeSinceStartupAsDouble < deadline) await Task.Yield();
            if (!loaded || epoch != liveEpoch) { connecting = false; SetStatus("Could not load the gallery's collection settings. Try Connect again."); UpdateControls(); return; }
            try
            {
                ServerBaseUrl = BaseUrl(); connection.BaseUrl = ServerBaseUrl;
                sessionTurns = 0; currentTurn = 0; take = null; answer.Clear(); replying = false;
                var settings = (JObject)profile.DeepClone(); settings.Remove("id");
                settings["type"] = "start"; settings["engine"] = Engine; settings["interruptions"] = false;
                await connection.ConnectAsync(settings);
            }
            catch { if (epoch == liveEpoch && this) SetStatus("The call could not start. Check the selected engine, server and tunnel."); }
            finally { if (epoch == liveEpoch && this) { connecting = false; UpdateControls(); } }
        }

        IEnumerator PrepareProfile(Action finished)
        {
            yield return GetJson("/api/archive/collections", data =>
            {
                var ids = new List<string>();
                foreach (var item in (data["collections"] as JArray ?? new JArray()))
                {
                    var kind = Text(item["source_kind"], 80); var id = Text(item["id"], 64);
                    if ((kind == "nga" || kind.StartsWith("museum-", StringComparison.Ordinal)) && item.Value<long?>("count") > 0 && ids.Count < 8) ids.Add(id);
                }
                profile["archive"] = new JObject { ["enabled"] = ids.Count > 0, ["scope"] = "library", ["entry_ids"] = new JArray(), ["collection_ids"] = new JArray(ids) };
                profile["director"]["enabled"] = true;
                finished();
            }, finished);
        }

        async Task Disconnect()
        {
            ++liveEpoch; ++imageEpoch; sessionReady = false; replying = false; connecting = false;
            Microphone.StopCapture(); Player.Clear(); take = null;
            await connection.DisconnectAsync(); SetStatus("Call ended. Your comparison results are kept until you leave Play mode."); UpdateControls(); nextPoll = 0;
        }

        void ToggleMicrophone()
        {
            if (!sessionReady) return;
            if (Microphone.IsRecording) { Microphone.StopCapture(); _ = Control(new JObject { ["type"] = "mute" }); SetStatus("Microphone off — you can still type."); }
            else
            {
                try { SetStatus("Listening — speak, then pause for Robin. Use Stop reply to take over."); Microphone.StartCapture(); }
                catch { SetStatus("The microphone could not start. Check Unity's microphone permission or use text."); }
            }
            UpdateControls();
        }

        async Task SubmitText()
        {
            string prompt = View.PromptInput.text.Trim(); if (!sessionReady || replying || prompt.Length == 0) return;
            prompt = prompt.Substring(0, Math.Min(2000, prompt.Length)); pendingQuestion = prompt;
            View.PromptInput.SetTextWithoutNotify(""); replying = true; Microphone.SetPaused(true); UpdateControls();
            await Control(new JObject { ["type"] = "text", ["text"] = prompt });
        }

        async Task Audio(byte[] packet)
        {
            int epoch = liveEpoch;
            if (audioQueued >= 64)
            {
                Microphone.StopCapture(); await Disconnect();
                if (this) SetStatus("The connection was too slow to send microphone audio reliably. Reconnect and try again.");
                return;
            }
            audioQueued++;
            try { await connection.SendAudioAsync(packet); }
            catch { if (epoch == liveEpoch && this) Fail("Microphone audio could not reach the server. Reconnect the call."); }
            finally { audioQueued--; }
        }
        async Task Control(JObject value) { int epoch = liveEpoch; try { if (connection.Connected) await connection.SendAsync(value); } catch { if (epoch == liveEpoch && this) Fail("The server connection stopped. Reconnect the call."); } }
        async Task Interrupt()
        {
            if (!sessionReady) return; Player.Clear(); take = null; replying = false; resumeAt = Time.realtimeSinceStartupAsDouble + .5;
            await Control(new JObject { ["type"] = "interrupt" }); SetStatus("Reply stopped. You can speak again."); UpdateControls();
        }

        void Message(JObject data)
        {
            string type = Text(data["type"], 80); int turn = data.Value<int?>("turn_id") ?? 0;
            if (type == "ready")
            {
                Player.Configure(data.Value<int?>("output_rate") ?? 44100); sessionReady = true;
                SetStatus("Connected to " + Label(Engine) + ". Type, or turn on the microphone."); UpdateControls(); return;
            }
            if (type == "error") { Fail(Text(data["message"], 500)); return; }
            if (type == "empty_transcript") { replying = false; SetStatus("No clear speech was detected. Try again or type."); UpdateControls(); return; }
            if (type == "listening_state") { if ((string)data["phase"] == "error") Fail(Text(data["message"], 500)); return; }
            if (type == "speech_start") { SetStatus("Listening…"); return; }
            if (type == "turn_start")
            {
                if (turn <= 0 || turn < currentTurn) return;
                currentTurn = turn; ++sessionTurns; ++imageEpoch; answer.Clear(); replying = true; Player.BeginTurn(turn); Microphone.SetPaused(true);
                question = pendingQuestion; pendingQuestion = "";
                take = new Take { Engine = Engine, Turn = sessionTurns, Prompt = question, Settings = profile.ToString(Formatting.None), TurnStarted = Time.realtimeSinceStartupAsDouble };
                View.ReplyText.text = (question.Length > 0 ? "YOU\n" + question + "\n\n" : "") + "ROBIN\n";
                SetStatus("Robin is considering your question…"); UpdateControls(); return;
            }
            if (turn != currentTurn || currentTurn == 0) return;
            if (type == "transcript") { question = Text(data["text"], 2000); if (take != null) take.Prompt = question; RenderReply(); }
            else if (type == "text_delta") { if (answer.Length < 16000) answer.Append(Text(data["text"], 16000 - answer.Length)); RenderReply(); }
            else if (type == "text_done") { answer.Clear(); answer.Append(Text(data["text"])); RenderReply(); }
            else if (type == "phrase_start") SetStatus(data.Value<bool?>("streaming") == true ? "Robin is speaking — streaming audio" : "Robin is preparing the voice reply…");
            else if (type == "phrase_done") Player.PhraseDone(data.Value<int?>("seq") ?? 0);
            else if (type == "response_done")
            {
                answer.Clear(); answer.Append(Text(data["text"])); RenderReply();
                if (take != null) { take.Complete = true; take.Reply = answer.ToString(); }
                Player.ResponseDone();
            }
            else if (type == "metrics")
            {
                if (take != null) take.Metrics.Merge(data, new JsonMergeSettings { MergeArrayHandling = MergeArrayHandling.Replace });
                View.TimingsText.text = Timing(data);
            }
            else if (type == "archive_sources")
            {
                if (take != null) take.Sources = string.Join(", ", (data["matches"] as JArray ?? new JArray()).Select(entry => Text(entry["collection_id"], 80) + ":" + Text(entry["id"], 128)));
                StartCoroutine(ShowArtworks(data, ++imageEpoch));
            }
            else if (type == "interrupted") { Player.Clear(); replying = false; take = null; SetStatus("Reply stopped."); UpdateControls(); }
            else if (type == "voice_notice") SetStatus(Text(data["message"], 500));
        }

        void RenderReply() { View.ReplyText.text = "YOU\n" + question + "\n\nROBIN\n" + answer; }
        void PlaybackFinished(int turn)
        {
            if (turn != currentTurn || !sessionReady) return;
            if (take != null && take.Complete)
            {
                take.Heard = true;
                take.PlaybackEstimateMs = double.IsNaN(Player.FirstSoundRealtime) || double.IsInfinity(Player.FirstSoundRealtime)
                    ? double.NaN : Math.Max(0, (Player.FirstSoundRealtime - take.TurnStarted) * 1000);
                comparisons[take.Engine] = take;
            }
            _ = Control(new JObject { ["type"] = "playback_done", ["turn_id"] = turn });
            replying = false; resumeAt = Time.realtimeSinceStartupAsDouble + .5;
            SetStatus(Microphone.IsRecording ? "Listening — what would you like to explore next?" : "Reply finished. Type, or turn on the microphone."); UpdateControls();
        }
        void Fail(string message)
        {
            replying = false; take = null; Player.Clear(); Microphone.SetPaused(true); resumeAt = Time.realtimeSinceStartupAsDouble + .5;
            if (!connection.Connected) { sessionReady = false; Microphone.StopCapture(); }
            SetStatus(string.IsNullOrWhiteSpace(message) ? "The call could not continue. Reconnect and try again." : message); UpdateControls();
        }

        static string Timing(JObject data)
        {
            var value = data.Value<double?>("speech_end_to_first_audio_ms") ?? data.Value<double?>("first_audio_ms");
            return value.HasValue ? "Server estimate to first audio: " + (value.Value / 1000).ToString("0.00") + " s" : "";
        }
        void ShowComparison()
        {
            var text = new StringBuilder("Same Robin, director and selected museum collections. Run one GPU stack at a time.\n\n");
            foreach (string engine in new[] { "baseline", "qwen-omni" })
            {
                text.AppendLine(Label(engine));
                if (!comparisons.TryGetValue(engine, out var sample)) { text.AppendLine("No completed, heard reply yet.\n"); continue; }
                text.AppendLine("You: " + sample.Prompt).AppendLine("Robin: " + sample.Reply).AppendLine(Timing(sample.Metrics));
                text.AppendLine("Playback-start estimate after turn event: " + (double.IsNaN(sample.PlaybackEstimateMs) ? "not measured" : (sample.PlaybackEstimateMs / 1000).ToString("0.00") + " s"));
                text.AppendLine("Archive records: " + (sample.Sources.Length > 0 ? sample.Sources : "none for this turn")).AppendLine();
            }
            if (comparisons.TryGetValue("baseline", out var first) && comparisons.TryGetValue("qwen-omni", out var second))
                text.AppendLine(first.Prompt == second.Prompt && first.Settings == second.Settings && first.Turn == 1 && second.Turn == 1 ? "Same first prompt and authored settings. Compare the voices, wording and sources yourself." : "Different prompt, settings or prior conversation. Repeat the same first question in two fresh calls for a controlled comparison.");
            text.AppendLine("Figures are software estimates, not measured microphone-to-speaker latency. Voices differ between engines. Results stay in this Play session; a new reply replaces that engine's previous sample.");
            View.ShowComparison(text.ToString());
        }

        static bool PublicUrl(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length > 2048 || !Uri.TryCreate(value, UriKind.Absolute, out var uri)) return false;
            var host = uri.DnsSafeHost.ToLowerInvariant();
            return uri.Scheme == "https" && uri.IsDefaultPort && uri.UserInfo.Length == 0 && uri.HostNameType == UriHostNameType.Dns && host.Contains(".") &&
                   !new[] { ".local", ".lan", ".internal", ".home", ".localhost" }.Any(host.EndsWith);
        }
        string ImageUrl(string value)
        {
            if (!PublicUrl(value)) return "";
            const string aic = "https://www.artic.edu/iiif/2/";
            if (value.StartsWith(aic, StringComparison.Ordinal))
            {
                var part = value.Substring(aic.Length).Split('/')[0];
                if (Guid.TryParse(part, out _) && value.EndsWith("/full/400,/0/default.jpg", StringComparison.Ordinal)) return ServerBaseUrl + "/api/archive/museum-images/aic/" + part;
            }
            if (value.StartsWith("https://api.nga.gov/iiif/", StringComparison.Ordinal)) return value.Replace("/full/!200,200/", "/full/!1200,1200/");
            return value;
        }
        IEnumerator ShowArtworks(JObject data, int epoch)
        {
            var records = (data["matches"] as JArray ?? new JArray()).Take(3).OfType<JObject>().ToList();
            bool requested = data.Value<bool?>("image_request") == true;
            int total = records.Count(entry => !string.IsNullOrEmpty(Text(entry["image_url"], 2048))), loaded = 0;
            if (records.Count == 0 && !requested) yield break;
            sourceUrl = "";
            View.SetArtwork(null, records.Count > 0 ? Text(records[0]["title"], 300) : "Looking in the collection", "Retrieving the museum image…");
            foreach (Transform child in View.ThumbnailRoot) Destroy(child.gameObject);
            foreach (var texture in textures) Destroy(texture); textures.Clear();
            if (requested && total > 0) ImageStatus("loading", 0, total, epoch);
            bool displayed = false;
            foreach (var record in records)
            {
                if (epoch != imageEpoch || !sessionReady) yield break;
                Texture2D texture = null; string url = ImageUrl(Text(record["image_url"], 2048));
                if (url.Length > 0)
                {
                    Task<byte[]> download = null;
                    try { download = web.GetBytesAsync(url, 20 * 1024 * 1024); } catch { }
                    if (download != null)
                    {
                        while (!download.IsCompleted) yield return null;
                        if (!download.IsCanceled && !download.IsFaulted)
                        {
                            texture = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                            bool valid = false;
                            try { valid = texture.LoadImage(download.Result, true) && texture.width <= 8192 && texture.height <= 8192; } catch { }
                            if (!valid) { Destroy(texture); texture = null; }
                        }
                        else { var observed = download.Exception; }
                    }
                }
                if (epoch != imageEpoch || !sessionReady) { if (texture) Destroy(texture); yield break; }
                if (texture) { textures.Add(texture); loaded++; }
                AddThumbnail(record, texture);
                if (!displayed && texture) { ShowArtwork(record, texture); displayed = true; }
                if (requested && loaded > 0) { yield return null; ImageStatus("loaded", loaded, total, epoch); }
            }
            if (!displayed)
            {
                var first = records.FirstOrDefault();
                sourceUrl = first != null && PublicUrl(Text(first["source_url"], 2048)) ? Text(first["source_url"], 2048) : "";
                View.SetArtwork(null, first == null ? "No artwork found" : Text(first["title"], 300), "No image could be loaded for these records. Try another work or open the museum record.", Text(first?["collection_label"], 100), Text(first?["metadata"]?["rights"], 1000), sourceUrl);
                UpdateControls();
            }
            if (requested && loaded == 0) ImageStatus("unavailable", 0, total, epoch);
        }
        void ImageStatus(string state, int loaded, int total, int epoch)
        {
            if (epoch != imageEpoch || !sessionReady) return;
            _ = Control(new JObject { ["type"] = "image_status", ["turn_id"] = currentTurn, ["status"] = state, ["loaded_count"] = loaded, ["total_count"] = total });
        }
        void ShowArtwork(JObject record, Texture2D texture)
        {
            var metadata = record["metadata"] as JObject ?? new JObject();
            sourceUrl = PublicUrl(Text(record["source_url"], 2048)) ? Text(record["source_url"], 2048) : "";
            View.SetArtwork(texture, Text(record["title"], 300), string.Join(" · ", new[] { Text(metadata["artist"], 200), Text(metadata["date"], 100), Text(metadata["medium"], 200) }.Where(value => value.Length > 0)), Text(record["collection_label"], 100), Text(metadata["rights"], 1000), sourceUrl);
            UpdateControls();
        }
        void AddThumbnail(JObject record, Texture2D texture)
        {
            var item = new GameObject("Artwork choice", typeof(RectTransform), typeof(UnityEngine.UI.Image), typeof(UnityEngine.UI.Button), typeof(UnityEngine.UI.LayoutElement));
            item.transform.SetParent(View.ThumbnailRoot, false);
            var layout = item.GetComponent<UnityEngine.UI.LayoutElement>(); layout.preferredWidth = 100; layout.preferredHeight = 110; layout.flexibleHeight = 0;
            item.GetComponent<UnityEngine.UI.Image>().color = new Color(.19f, .16f, .13f);
            var picture = new GameObject("Thumbnail", typeof(RectTransform), typeof(UnityEngine.UI.RawImage)); picture.transform.SetParent(item.transform, false);
            var rect = (RectTransform)picture.transform; rect.anchorMin = new Vector2(.08f, .33f); rect.anchorMax = new Vector2(.92f, .94f); rect.offsetMin = rect.offsetMax = Vector2.zero;
            var image = picture.GetComponent<UnityEngine.UI.RawImage>(); image.texture = texture; image.raycastTarget = false; image.color = texture ? Color.white : new Color(.24f, .21f, .17f);
            var label = new GameObject("Title", typeof(RectTransform), typeof(TextMeshProUGUI)); label.transform.SetParent(item.transform, false);
            var labelRect = (RectTransform)label.transform; labelRect.anchorMin = new Vector2(.05f, .02f); labelRect.anchorMax = new Vector2(.95f, .31f); labelRect.offsetMin = labelRect.offsetMax = Vector2.zero;
            var caption = label.GetComponent<TextMeshProUGUI>(); caption.font = View.ReplyText.font; caption.fontSize = 12; caption.richText = false; caption.raycastTarget = false; caption.color = new Color(.83f, .77f, .65f); caption.text = Text(record["title"], 120); caption.overflowMode = TextOverflowModes.Ellipsis;
            item.GetComponent<UnityEngine.UI.Button>().onClick.AddListener(() => ShowArtwork(record, texture));
        }

        void SetStatus(string text) { if (View && View.StatusText) View.StatusText.text = text; }
        static void ButtonText(UnityEngine.UI.Button button, string text) { var label = button.GetComponentInChildren<TMP_Text>(); if (label) label.text = text; }
        void UpdateControls()
        {
            if (!View) return;
            View.ConnectButton.interactable = !connecting && (connection.Connected || Ready()); ButtonText(View.ConnectButton, connection.Connected ? "End call" : connecting ? "Connecting…" : "Connect");
            View.ServerInput.interactable = !connection.Connected && !connecting;
            View.TalkButton.interactable = sessionReady; ButtonText(View.TalkButton, Microphone && Microphone.IsRecording ? "Microphone on" : "Start microphone");
            View.SendButton.interactable = sessionReady && !replying; View.PromptInput.interactable = sessionReady;
            View.StopButton.interactable = sessionReady && replying;
            View.EngineDropdown.interactable = !connecting;
            if (View.EngineNote) View.EngineNote.text = Label(Engine) + (capabilities?["comparison"]?["engines"]?[Engine]?["ready"]?.Value<bool>() == true ? " · ready" : " · start this stack on Surrey") + ". The same Robin and imported art collections are used.";
            if (View.SourceButton) View.SourceButton.interactable = PublicUrl(sourceUrl);
        }
        void OnDestroy()
        {
            ++liveEpoch; ++imageEpoch;
            if (Microphone) Microphone.StopCapture(); if (Player) Player.Clear(); connection.Dispose(); web.Dispose();
            foreach (var texture in textures) if (texture) Destroy(texture);
            if (backgroundCaptured) Application.runInBackground = previousBackground;
        }
    }
}
