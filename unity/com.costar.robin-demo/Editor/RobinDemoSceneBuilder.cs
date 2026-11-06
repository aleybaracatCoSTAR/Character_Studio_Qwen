#if UNITY_EDITOR
using System.IO;
using TMPro;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.InputSystem.UI;
using UnityEngine.SceneManagement;
using UnityEngine.TextCore.LowLevel;

namespace CoSTAR.RobinDemo.Editor
{
    /// <summary>Creates a separate, editable demonstration scene without replacing the open scene.</summary>
    public static class RobinDemoSceneBuilder
    {
        private const string Generated = "Assets/RobinDemo/Generated";
        private static readonly Color Paper = Hex("EEE2CD");
        private static readonly Color Muted = Hex("BCA88A");
        private static readonly Color Ink = Hex("30251D");
        private static TMP_FontAsset bodyFont;
        private static TMP_FontAsset titleFont;
        private static TMP_FontAsset blackFont;

        [MenuItem("Tools/CoSTAR/Create Robin Archive Demo")]
        public static void CreateScene()
        {
            Directory.CreateDirectory(Generated + "/Fonts");
            Directory.CreateDirectory("Assets/RobinDemo/Scenes");
            AssetDatabase.Refresh();
            EnsureTextResources();
            bodyFont = DefaultBodyFont();
            titleFont = FontAsset(Resources.Load<Font>("Fonts/Avara-Bold"), "AvaraBold") ?? bodyFont;
            blackFont = FontAsset(Resources.Load<Font>("Fonts/Avara-Black"), "AvaraBlack") ?? titleFont;
            bodyFont = bodyFont ?? titleFont;
            if (!UsableFont(bodyFont) || !UsableFont(titleFont) || !UsableFont(blackFont))
                throw new System.InvalidOperationException("Robin demo could not load a usable TMP font. Wait for TMP Essential Resources to finish importing, then run Create Robin Archive Demo again. Ensure the supplied Avara font files include font data. No scene has been replaced.");

            // Additive creation preserves every existing scene and its unsaved state.
            // A unique filename also preserves earlier edited Robin demo scenes.
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Additive);
            SceneManager.SetActiveScene(scene);
            var root = new GameObject("Robin Archive Demo");
            var view = root.AddComponent<RobinDemoView>();
            var audio = root.AddComponent<AudioSource>();
            audio.playOnAwake = false;
            audio.spatialBlend = 0;
            audio.volume = .85f;
            var player = root.AddComponent<RobinAudioPlayer>();
            var microphone = root.AddComponent<RobinMicrophone>();
            var controller = root.AddComponent<RobinGalleryDemo>();

            var camera = CreateRoom(root.transform, view);
            var canvasObject = new GameObject("Gallery Interface", typeof(RectTransform), typeof(Canvas),
                typeof(UnityEngine.UI.CanvasScaler), typeof(UnityEngine.UI.GraphicRaycaster));
            canvasObject.transform.SetParent(root.transform, false);
            var canvas = canvasObject.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 100;
            var scaler = canvasObject.GetComponent<UnityEngine.UI.CanvasScaler>();
            scaler.uiScaleMode = UnityEngine.UI.CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1920, 1080);
            scaler.matchWidthOrHeight = .5f;

            BuildHeader(canvasObject.transform);
            BuildVisitRail(canvasObject.transform, view);
            BuildConversation(canvasObject.transform, view);
            BuildComparison(canvasObject.transform, view);

            var events = new GameObject("Robin Event System", typeof(UnityEngine.EventSystems.EventSystem), typeof(InputSystemUIInputModule));
            events.transform.SetParent(root.transform, false);
            events.GetComponent<InputSystemUIInputModule>().AssignDefaultActions();

            controller.View = view;
            controller.Player = player;
            controller.Microphone = microphone;
            controller.DefaultProfileText = Resources.Load<TextAsset>("robin-profile");
            controller.ServerBaseUrl = "http://127.0.0.1:18091";
            view.ServerInput.text = controller.ServerBaseUrl;
            if (controller.DefaultProfileText == null)
                Debug.LogWarning("Robin demo: Resources/robin-profile.json was not found. The scene is still editable; add the supplied profile before connecting.");

            var path = AssetDatabase.GenerateUniqueAssetPath("Assets/RobinDemo/Scenes/RobinArchiveDemo.unity");
            EditorSceneManager.SaveScene(scene, path);
            AssetDatabase.SaveAssets();
            Selection.activeGameObject = root;
            SceneView.lastActiveSceneView?.LookAt(new Vector3(-1.5f, 2.6f, 3.7f), Quaternion.Euler(0, 0, 0), 9);
            Debug.Log("Created " + path + ". Existing scenes remain open and unchanged. Open this saved demo scene on its own before Play to avoid duplicate cameras or EventSystems.");
        }

        private static Camera CreateRoom(Transform parent, RobinDemoView view)
        {
            var architecture = new GameObject("Gallery Room").transform;
            architecture.SetParent(parent, false);
            var wall = Material("WarmPlaster", Hex("46372B"));
            var floor = Material("SmokedOak", Hex("2B221B"));
            var dark = Material("FrameWalnut", Hex("241A12"));
            var brass = Material("FrameBrass", Hex("8D6C42"), .45f);
            var matte = Material("GalleryMat", Hex("DFD2B8"));
            Cube("Back Wall", architecture, new Vector3(0, 3.4f, 4.45f), new Vector3(19, 7, .3f), wall);
            Cube("Floor", architecture, new Vector3(0, -.16f, 3), new Vector3(19, .3f, 19), floor);
            Cube("Left Return", architecture, new Vector3(-9.3f, 3.4f, 0), new Vector3(.3f, 7, 9), wall);
            Cube("Picture Rail", architecture, new Vector3(0, .28f, 4.22f), new Vector3(19, .06f, .08f), brass);
            for (int i = 0; i < 12; i++)
                Cube("Oak Join " + i, architecture, new Vector3(-8.5f + i * 1.5f, .001f, 0), new Vector3(.015f, .012f, 14), dark);

            Vector3 centre = new Vector3(-2.1f, 2.8f, 4.08f);
            Cube("Framed Artwork / Back", architecture, centre, new Vector3(6.5f, 4.85f, .18f), dark);
            Cube("Framed Artwork / Mat", architecture, centre + new Vector3(0, 0, -.105f), new Vector3(6.17f, 4.52f, .05f), matte);
            Cube("Frame / Top", architecture, centre + new Vector3(0, 2.38f, -.16f), new Vector3(6.5f, .12f, .15f), brass);
            Cube("Frame / Bottom", architecture, centre + new Vector3(0, -2.38f, -.16f), new Vector3(6.5f, .12f, .15f), brass);
            Cube("Frame / Left", architecture, centre + new Vector3(-3.19f, 0, -.16f), new Vector3(.12f, 4.65f, .15f), brass);
            Cube("Frame / Right", architecture, centre + new Vector3(3.19f, 0, -.16f), new Vector3(.12f, 4.65f, .15f), brass);

            var cameraObject = new GameObject("Robin Gallery Camera", typeof(Camera), typeof(AudioListener));
            cameraObject.transform.SetParent(parent, false);
            cameraObject.tag = "MainCamera";
            cameraObject.transform.position = new Vector3(0, 2.85f, -7.7f);
            cameraObject.transform.LookAt(new Vector3(0, 2.7f, 4));
            var camera = cameraObject.GetComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = Hex("211A15");
            camera.fieldOfView = 38;
            camera.nearClipPlane = .1f;
            camera.farClipPlane = 50;
            camera.depth = 50;
            camera.allowHDR = false;

            var world = new GameObject("Artwork Surface", typeof(RectTransform), typeof(Canvas));
            world.transform.SetParent(architecture, false);
            world.transform.position = centre + new Vector3(0, 0, -.16f);
            world.transform.localScale = Vector3.one * .01f;
            var worldCanvas = world.GetComponent<Canvas>();
            worldCanvas.renderMode = RenderMode.WorldSpace;
            worldCanvas.worldCamera = camera;
            var worldRect = world.GetComponent<RectTransform>();
            worldRect.sizeDelta = new Vector2(596, 430);
            var mount = Rect("Image Bounds", world.transform);
            Stretch(mount);
            var picture = Rect("Museum Artwork", mount);
            Stretch(picture);
            view.ArtworkImage = picture.gameObject.AddComponent<UnityEngine.UI.RawImage>();
            view.ArtworkImage.raycastTarget = false;
            view.ArtworkImage.enabled = false;
            var fitter = picture.gameObject.AddComponent<UnityEngine.UI.AspectRatioFitter>();
            fitter.aspectMode = UnityEngine.UI.AspectRatioFitter.AspectMode.FitInParent;
            fitter.aspectRatio = 1.4f;
            view.ArtworkPlaceholder = Text("Your next discovery", world.transform, titleFont, 29, Hex("554330"), 160);
            Stretch(view.ArtworkPlaceholder.rectTransform, new Vector2(50, 85), new Vector2(-50, -85));
            view.ArtworkPlaceholder.alignment = TextAlignmentOptions.Center;

            var lighting = new GameObject("Gallery Light", typeof(Light));
            lighting.transform.SetParent(parent, false);
            lighting.transform.rotation = Quaternion.Euler(44, -22, 0);
            var key = lighting.GetComponent<Light>();
            key.type = LightType.Directional;
            key.color = Hex("FFE5BC");
            key.intensity = 1.25f;
            key.shadows = LightShadows.Soft;
            var spotObject = new GameObject("Artwork Wash", typeof(Light));
            spotObject.transform.SetParent(parent, false);
            spotObject.transform.position = new Vector3(-2.1f, 6, -.7f);
            spotObject.transform.LookAt(centre);
            var spot = spotObject.GetComponent<Light>();
            spot.type = LightType.Spot;
            spot.range = 14;
            spot.spotAngle = 68;
            spot.color = Hex("FFDDAA");
            spot.intensity = 2.2f;
            RenderSettings.ambientLight = Hex("6C5844");
            return camera;
        }

        private static void BuildHeader(Transform canvas)
        {
            var header = Rect("Header", canvas);
            Area(header, new Vector2(.035f, .925f), new Vector2(.965f, .99f));
            var left = Text("ART ARCHIVES", header, bodyFont, 15, Muted, 32);
            Area(left.rectTransform, Vector2.zero, new Vector2(.3f, 1));
            left.alignment = TextAlignmentOptions.MidlineLeft;
            var brand = Text("Character Studio", header, blackFont, 37, Paper, 48);
            Area(brand.rectTransform, new Vector2(.28f, 0), new Vector2(.7f, 1));
            brand.alignment = TextAlignmentOptions.Center;
            var label = Text("ROBIN  /  A GALLERY IN CONVERSATION", header, bodyFont, 12, Muted, 32);
            Area(label.rectTransform, new Vector2(.7f, 0), Vector2.one);
            label.alignment = TextAlignmentOptions.MidlineRight;
        }

        private static void BuildVisitRail(Transform canvas, RobinDemoView view)
        {
            var rail = Panel("Your Visit", canvas, new Color(.12f, .09f, .07f, .88f));
            Area(rail, new Vector2(.025f, .105f), new Vector2(.105f, .87f));
            var layout = Vertical(rail, 16, 18);
            layout.childAlignment = TextAnchor.UpperCenter;
            Text("YOUR VISIT", rail, bodyFont, 12, Muted, 30).alignment = TextAlignmentOptions.Center;
            var scroll = Scroll("Artwork Thumbnails", rail, out var content);
            Flex(scroll.GetComponent<RectTransform>(), 1);
            var contentLayout = content.GetComponent<UnityEngine.UI.VerticalLayoutGroup>();
            contentLayout.spacing = 14;
            contentLayout.childAlignment = TextAnchor.UpperCenter;
            view.ThumbnailRoot = content;
        }

        private static void BuildConversation(Transform canvas, RobinDemoView view)
        {
            var panel = Panel("Artwork and Conversation", canvas, new Color(.16f, .12f, .095f, .96f));
            Area(panel, new Vector2(.7f, .035f), new Vector2(.975f, .905f));
            Vertical(panel, 24, 8);
            Text("IN THE COLLECTION", panel, bodyFont, 12, Muted, 20);
            view.ArtworkTitle = Text("A closer look", panel, titleFont, 34, Paper, 90);
            view.ArtworkTitle.maxVisibleLines = 3;
            view.ArtworkTitle.overflowMode = TextOverflowModes.Ellipsis;
            view.ArtworkTitle.enableAutoSizing = true;
            view.ArtworkTitle.fontSizeMin = 24;
            view.ArtworkTitle.fontSizeMax = 34;
            view.ArtworkMetadata = Text("Your conversation leads the way. Museum catalogue details appear here when an artwork is found.", panel, bodyFont, 15, Muted, 44);
            view.ArtworkCollection = Text("SAVED ART COLLECTIONS", panel, bodyFont, 11, Muted, 18);
            view.SourceButton = Button("View museum record ↗", panel, false);
            Preferred(view.SourceButton.GetComponent<RectTransform>(), 29);
            view.SourceButton.gameObject.SetActive(false);
            view.ArtworkRights = Text("", panel, bodyFont, 10, Muted, 17);
            Text("CONVERSATION", panel, bodyFont, 12, Muted, 22);
            var engineRow = Rect("Engine", panel);
            Preferred(engineRow, 39);
            view.EngineDropdown = Dropdown(engineRow);
            Stretch(view.EngineDropdown.GetComponent<RectTransform>());

            var chat = Scroll("Conversation Scroll", panel, out var chatContent);
            Flex(chat.GetComponent<RectTransform>(), 1, 110);
            view.ConversationScroll = chat;
            view.ReplyText = Text("Robin\nWhat catches your eye? Ask about an artist, an artwork or a feeling.", chatContent, bodyFont, 19, Paper, -1);
            view.ReplyText.margin = new Vector4(0, 7, 10, 15);
            view.ReplyText.overflowMode = TextOverflowModes.Overflow;

            var promptRow = Rect("Message", panel);
            Preferred(promptRow, 65);
            Horizontal(promptRow, 8);
            view.PromptInput = Input("Ask Robin…", promptRow, true);
            Flex(view.PromptInput.GetComponent<RectTransform>(), 1);
            view.SendButton = Button("Send", promptRow, true);
            Width(view.SendButton.GetComponent<RectTransform>(), 78);

            var buttons = Rect("Call Controls", panel);
            Preferred(buttons, 43);
            Horizontal(buttons, 8);
            view.TalkButton = Button("Start microphone", buttons, true);
            Flex(view.TalkButton.GetComponent<RectTransform>(), 1.4f);
            view.StopButton = Button("Stop reply", buttons, false);
            Flex(view.StopButton.GetComponent<RectTransform>(), 1);
            view.CompareButton = Button("Compare replies", buttons, false);
            Flex(view.CompareButton.GetComponent<RectTransform>(), 1.1f);

            var connection = Rect("Server Connection", panel);
            Preferred(connection, 38);
            Horizontal(connection, 8);
            view.ServerInput = Input("http://127.0.0.1:18091", connection, false);
            Flex(view.ServerInput.GetComponent<RectTransform>(), 1);
            view.ConnectButton = Button("Connect", connection, false);
            Width(view.ConnectButton.GetComponent<RectTransform>(), 88);
            view.StatusText = Text("Connect to your gallery server when you are ready.", panel, bodyFont, 12, Muted, 27);
            view.EngineNote = Text("Same character, director and collections. Choose an engine before connecting.", panel, bodyFont, 11, Muted, 32);
            view.TimingsText = Text("", panel, bodyFont, 10, Muted, 27);

            var motion = Rect("Motion Preference", panel);
            Preferred(motion, 23);
            view.ReducedMotionToggle = motion.gameObject.AddComponent<UnityEngine.UI.Toggle>();
            var box = Panel("Box", motion, Hex("65513B"));
            box.anchorMin = box.anchorMax = new Vector2(0, .5f);
            box.pivot = new Vector2(0, .5f);
            box.sizeDelta = new Vector2(16, 16);
            var check = Panel("Check", box, Hex("E5CFAD"));
            Stretch(check, Vector2.one * 4, Vector2.one * -4);
            view.ReducedMotionToggle.targetGraphic = box.GetComponent<UnityEngine.UI.Image>();
            view.ReducedMotionToggle.graphic = check.GetComponent<UnityEngine.UI.Image>();
            view.ReducedMotionToggle.isOn = false;
            var motionLabel = Text("Reduced motion", motion, bodyFont, 11, Muted, 20);
            Stretch(motionLabel.rectTransform, new Vector2(24, 0), Vector2.zero);

            var replay = Button("Replay artwork reveal", canvas, false);
            Area(replay.GetComponent<RectTransform>(), new Vector2(.27f, .05f), new Vector2(.56f, .088f));
            replay.onClick.AddListener(view.ReplayReveal);
        }

        private static void BuildComparison(Transform canvas, RobinDemoView view)
        {
            var overlay = Panel("Comparison Overlay", canvas, new Color(.05f, .035f, .025f, .86f));
            Stretch(overlay);
            overlay.GetComponent<UnityEngine.UI.Image>().raycastTarget = true;
            view.ComparisonPanel = overlay.gameObject;
            var panel = Panel("Comparison", overlay, Ink);
            Area(panel, new Vector2(.22f, .16f), new Vector2(.78f, .84f));
            Vertical(panel, 34, 18);
            Text("Compare replies", panel, titleFont, 36, Paper, 60);
            Text("Try the same question with each engine. Timings are estimates; judge the character and its grounding yourself.", panel, bodyFont, 17, Muted, 64);
            var scroll = Scroll("Comparison Results", panel, out var content);
            Flex(scroll.GetComponent<RectTransform>(), 1);
            view.ComparisonText = Text("Complete a reply with each engine to compare it here.", content, bodyFont, 18, Paper, -1);
            view.CloseComparisonButton = Button("Back to the gallery", panel, true);
            Preferred(view.CloseComparisonButton.GetComponent<RectTransform>(), 44);
            overlay.gameObject.SetActive(false);
        }

        private static void EnsureTextResources()
        {
            if (TMP_Settings.LoadDefaultSettings() != null && Shader.Find("TextMeshPro/Mobile/Distance Field") != null) return;
            // Canonical TMP importer: essential assets only, without the blocking
            // import window or the unrelated Examples & Extras package.
            TMP_PackageResourceImporter.ImportResources(true, false, false);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            if (TMP_Settings.LoadDefaultSettings() == null || Shader.Find("TextMeshPro/Mobile/Distance Field") == null)
                throw new System.InvalidOperationException("TMP Essential Resources are being imported. Wait for Unity's import to finish, then run Tools > CoSTAR > Create Robin Archive Demo again. Existing scenes are unchanged.");
        }

        private static TMP_FontAsset DefaultBodyFont()
        {
            if (TMP_Settings.LoadDefaultSettings() != null && UsableFont(TMP_Settings.defaultFontAsset))
                return TMP_Settings.defaultFontAsset;
            var liberation = Resources.Load<TMP_FontAsset>("Fonts & Materials/LiberationSans SDF");
            if (UsableFont(liberation)) return liberation;
            foreach (var guid in AssetDatabase.FindAssets("LiberationSans t:TMP_FontAsset"))
            {
                var font = AssetDatabase.LoadAssetAtPath<TMP_FontAsset>(AssetDatabase.GUIDToAssetPath(guid));
                if (UsableFont(font)) return font;
            }
            // Real imported font data can be read by TextCore. Unity 6's built-in
            // LegacyRuntime font is not guaranteed to expose a loadable face.
            return FontAsset(Resources.Load<Font>("Fonts/Avara-Bold"), "AvaraBold");
        }

        private static bool UsableFont(TMP_FontAsset font)
        {
            return font != null && font.material != null && font.material.shader != null
                && font.atlasTextures != null && font.atlasTextures.Length > 0 && font.atlasTextures[0] != null;
        }

        private static TMP_FontAsset FontAsset(Font font, string name)
        {
            if (font == null) return null;
            var path = Generated + "/Fonts/" + name + ".asset";
            var existing = AssetDatabase.LoadAssetAtPath<TMP_FontAsset>(path);
            if (UsableFont(existing)) return existing;
            if (existing != null)
            {
                Debug.LogWarning("Robin demo: existing font asset " + path + " is incomplete. It was preserved; the imported TMP default font will be used instead.");
                return null;
            }
            var asset = TMP_FontAsset.CreateFontAsset(font, 90, 9, GlyphRenderMode.SDFAA, 1024, 1024, AtlasPopulationMode.Dynamic, true);
            if (!UsableFont(asset))
            {
                if (asset != null) Object.DestroyImmediate(asset);
                Debug.LogWarning("Robin demo: TextCore could not create " + name + " from the supplied font. Check Include Font Data in that font's import settings. The imported TMP default font will be used instead.");
                return null;
            }
            asset.name = name;
            AssetDatabase.CreateAsset(asset, path);
            if (asset.material != null && !AssetDatabase.Contains(asset.material)) AssetDatabase.AddObjectToAsset(asset.material, asset);
            foreach (var texture in asset.atlasTextures)
                if (texture != null && !AssetDatabase.Contains(texture)) AssetDatabase.AddObjectToAsset(texture, asset);
            EditorUtility.SetDirty(asset);
            return asset;
        }

        private static RectTransform Rect(string name, Transform parent)
        {
            var node = new GameObject(name, typeof(RectTransform));
            node.transform.SetParent(parent, false);
            return node.GetComponent<RectTransform>();
        }

        private static RectTransform Panel(string name, Transform parent, Color color)
        {
            var rect = Rect(name, parent);
            var image = rect.gameObject.AddComponent<UnityEngine.UI.Image>();
            image.color = color;
            image.raycastTarget = false;
            return rect;
        }

        private static TMP_Text Text(string value, Transform parent, TMP_FontAsset font, float size, Color color, float height)
        {
            var rect = Rect("Text", parent);
            var text = rect.gameObject.AddComponent<TextMeshProUGUI>();
            text.font = font;
            text.text = value;
            text.fontSize = size;
            text.color = color;
            text.richText = false;
            text.raycastTarget = false;
            text.enableWordWrapping = true;
            text.overflowMode = TextOverflowModes.Ellipsis;
            if (height >= 0) Preferred(rect, height);
            return text;
        }

        private static UnityEngine.UI.Button Button(string label, Transform parent, bool primary)
        {
            var rect = Panel(label, parent, primary ? Hex("DFCAA7") : Hex("493726"));
            var image = rect.GetComponent<UnityEngine.UI.Image>();
            image.raycastTarget = true;
            var button = rect.gameObject.AddComponent<UnityEngine.UI.Button>();
            button.targetGraphic = image;
            var colors = button.colors;
            colors.highlightedColor = new Color(1, .94f, .83f);
            colors.pressedColor = new Color(.8f, .7f, .56f);
            colors.selectedColor = new Color(1, .94f, .83f);
            button.colors = colors;
            var text = Text(label, rect, bodyFont, 14, primary ? Ink : Paper, 35);
            Stretch(text.rectTransform, new Vector2(8, 2), new Vector2(-8, -2));
            text.alignment = TextAlignmentOptions.Center;
            return button;
        }

        private static TMP_InputField Input(string placeholder, Transform parent, bool multiline)
        {
            var rect = Panel("Input", parent, Hex("211A14"));
            rect.GetComponent<UnityEngine.UI.Image>().raycastTarget = true;
            var field = rect.gameObject.AddComponent<TMP_InputField>();
            var viewport = Rect("Text Area", rect);
            Stretch(viewport, new Vector2(12, 8), new Vector2(-12, -8));
            viewport.gameObject.AddComponent<UnityEngine.UI.RectMask2D>();
            var text = Text("", viewport, bodyFont, multiline ? 18 : 13, Paper, -1);
            Stretch(text.rectTransform);
            var hint = Text(placeholder, viewport, bodyFont, multiline ? 18 : 13, Muted, -1);
            Stretch(hint.rectTransform);
            hint.fontStyle = FontStyles.Italic;
            field.textViewport = viewport;
            field.textComponent = (TextMeshProUGUI)text;
            field.placeholder = hint;
            field.lineType = multiline ? TMP_InputField.LineType.MultiLineNewline : TMP_InputField.LineType.SingleLine;
            field.characterLimit = multiline ? 2000 : 250;
            field.customCaretColor = true;
            field.caretColor = Paper;
            return field;
        }

        private static TMP_Dropdown Dropdown(Transform parent)
        {
            var rect = Panel("Voice Engine", parent, Hex("382A1E"));
            rect.GetComponent<UnityEngine.UI.Image>().raycastTarget = true;
            var dropdown = rect.gameObject.AddComponent<TMP_Dropdown>();
            var caption = Text("Current · Qwen + Drama 3", rect, bodyFont, 14, Paper, 35);
            Stretch(caption.rectTransform, new Vector2(11, 3), new Vector2(-26, -3));
            dropdown.captionText = (TextMeshProUGUI)caption;
            var arrow = Text("⌄", rect, bodyFont, 18, Muted, 24);
            Area(arrow.rectTransform, new Vector2(.91f, .1f), new Vector2(.99f, .9f));
            arrow.alignment = TextAlignmentOptions.Center;
            var template = Panel("Template", rect, Hex("483522"));
            template.anchorMin = new Vector2(0, 0);
            template.anchorMax = new Vector2(1, 0);
            template.pivot = new Vector2(.5f, 1);
            template.anchoredPosition = new Vector2(0, -4);
            template.sizeDelta = new Vector2(0, 88);
            var viewport = Panel("Viewport", template, Color.white);
            Stretch(viewport);
            viewport.gameObject.AddComponent<UnityEngine.UI.Mask>().showMaskGraphic = false;
            var content = Rect("Content", viewport);
            content.anchorMin = new Vector2(0, 1);
            content.anchorMax = Vector2.one;
            content.pivot = new Vector2(.5f, 1);
            content.sizeDelta = new Vector2(0, 42);
            var item = Panel("Item", content, Hex("483522"));
            Stretch(item);
            var toggle = item.gameObject.AddComponent<UnityEngine.UI.Toggle>();
            toggle.targetGraphic = item.GetComponent<UnityEngine.UI.Image>();
            var label = Text("Voice engine", item, bodyFont, 14, Paper, 40);
            Stretch(label.rectTransform, new Vector2(12, 2), new Vector2(-12, -2));
            dropdown.template = template;
            dropdown.itemText = (TextMeshProUGUI)label;
            dropdown.options = new System.Collections.Generic.List<TMP_Dropdown.OptionData> {
                new TMP_Dropdown.OptionData("Current · Qwen + Drama 3"), new TMP_Dropdown.OptionData("Streaming · Qwen3-Omni") };
            dropdown.RefreshShownValue();
            template.gameObject.SetActive(false);
            return dropdown;
        }

        private static UnityEngine.UI.ScrollRect Scroll(string name, Transform parent, out RectTransform content)
        {
            var rect = Rect(name, parent);
            var scroll = rect.gameObject.AddComponent<UnityEngine.UI.ScrollRect>();
            scroll.horizontal = false;
            scroll.movementType = UnityEngine.UI.ScrollRect.MovementType.Clamped;
            scroll.scrollSensitivity = 28;
            var viewport = Panel("Viewport", rect, Color.white);
            Stretch(viewport);
            viewport.GetComponent<UnityEngine.UI.Image>().raycastTarget = true;
            viewport.gameObject.AddComponent<UnityEngine.UI.Mask>().showMaskGraphic = false;
            content = Rect("Content", viewport);
            content.anchorMin = new Vector2(0, 1);
            content.anchorMax = Vector2.one;
            content.pivot = new Vector2(.5f, 1);
            content.sizeDelta = Vector2.zero;
            Vertical(content, 0, 8);
            var fitter = content.gameObject.AddComponent<UnityEngine.UI.ContentSizeFitter>();
            fitter.horizontalFit = UnityEngine.UI.ContentSizeFitter.FitMode.Unconstrained;
            fitter.verticalFit = UnityEngine.UI.ContentSizeFitter.FitMode.PreferredSize;
            scroll.viewport = viewport;
            scroll.content = content;
            return scroll;
        }

        private static UnityEngine.UI.VerticalLayoutGroup Vertical(RectTransform rect, int padding, float spacing)
        {
            var layout = rect.gameObject.AddComponent<UnityEngine.UI.VerticalLayoutGroup>();
            layout.padding = new RectOffset(padding, padding, padding, padding);
            layout.spacing = spacing;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = true;
            layout.childForceExpandHeight = false;
            return layout;
        }

        private static void Horizontal(RectTransform rect, float spacing)
        {
            var layout = rect.gameObject.AddComponent<UnityEngine.UI.HorizontalLayoutGroup>();
            layout.spacing = spacing;
            layout.childControlWidth = true;
            layout.childControlHeight = true;
            layout.childForceExpandWidth = false;
            layout.childForceExpandHeight = true;
        }

        private static UnityEngine.UI.LayoutElement Element(RectTransform rect)
        {
            return rect.GetComponent<UnityEngine.UI.LayoutElement>() ?? rect.gameObject.AddComponent<UnityEngine.UI.LayoutElement>();
        }

        private static void Preferred(RectTransform rect, float height) { var layout = Element(rect); layout.minHeight = height; layout.preferredHeight = height; }
        private static void Width(RectTransform rect, float width) { var layout = Element(rect); layout.minWidth = width; layout.preferredWidth = width; }
        private static void Flex(RectTransform rect, float weight, float minimum = 0) { var layout = Element(rect); layout.flexibleWidth = weight; layout.flexibleHeight = weight; layout.minHeight = minimum; }
        private static void Area(RectTransform rect, Vector2 minimum, Vector2 maximum) { rect.anchorMin = minimum; rect.anchorMax = maximum; rect.pivot = new Vector2(.5f, .5f); rect.offsetMin = Vector2.zero; rect.offsetMax = Vector2.zero; }
        private static void Stretch(RectTransform rect) { Stretch(rect, Vector2.zero, Vector2.zero); }
        private static void Stretch(RectTransform rect, Vector2 minimum, Vector2 maximum) { rect.anchorMin = Vector2.zero; rect.anchorMax = Vector2.one; rect.pivot = new Vector2(.5f, .5f); rect.offsetMin = minimum; rect.offsetMax = maximum; }

        private static void Cube(string name, Transform parent, Vector3 position, Vector3 size, Material material)
        {
            var cube = GameObject.CreatePrimitive(PrimitiveType.Cube);
            cube.name = name;
            cube.transform.SetParent(parent, false);
            cube.transform.localPosition = position;
            cube.transform.localScale = size;
            cube.GetComponent<Renderer>().sharedMaterial = material;
            Object.DestroyImmediate(cube.GetComponent<Collider>());
        }

        private static Material Material(string name, Color color, float metallic = 0)
        {
            var path = Generated + "/" + name + ".mat";
            var existing = AssetDatabase.LoadAssetAtPath<Material>(path);
            if (existing != null) return existing;
            var shader = UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline != null
                ? Shader.Find("Universal Render Pipeline/Lit") ?? Shader.Find("Standard")
                : Shader.Find("Standard") ?? Shader.Find("Universal Render Pipeline/Lit");
            shader = shader ?? Shader.Find("Unlit/Color");
            var material = new Material(shader) { name = name, color = color };
            if (material.HasProperty("_Metallic")) material.SetFloat("_Metallic", metallic);
            if (material.HasProperty("_Smoothness")) material.SetFloat("_Smoothness", .2f);
            if (material.HasProperty("_Glossiness")) material.SetFloat("_Glossiness", .2f);
            AssetDatabase.CreateAsset(material, path);
            return material;
        }

        private static Color Hex(string hex) { ColorUtility.TryParseHtmlString("#" + hex, out var color); return color; }
    }
}
#endif
