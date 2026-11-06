using System;
using System.Collections;
using TMPro;
using UnityEngine;

namespace CoSTAR.RobinDemo
{
    /// <summary>Presentation only: the gallery controller owns networking and speech.</summary>
    public sealed class RobinDemoView : MonoBehaviour
    {
        public TMP_InputField ServerInput;
        public TMP_InputField PromptInput;
        public TMP_Dropdown EngineDropdown;
        public UnityEngine.UI.Button ConnectButton;
        public UnityEngine.UI.Button TalkButton;
        public UnityEngine.UI.Button SendButton;
        public UnityEngine.UI.Button StopButton;
        public UnityEngine.UI.Button CompareButton;
        public TMP_Text StatusText;
        public TMP_Text ReplyText;
        public TMP_Text ArtworkTitle;
        public TMP_Text ArtworkMetadata;
        public TMP_Text TimingsText;
        public TMP_Text EngineNote;
        public UnityEngine.UI.RawImage ArtworkImage;
        public Transform ThumbnailRoot;

        [Header("Optional gallery presentation")]
        public TMP_Text ArtworkCollection;
        public TMP_Text ArtworkRights;
        public TMP_Text ArtworkPlaceholder;
        public UnityEngine.UI.Button SourceButton;
        public UnityEngine.UI.ScrollRect ConversationScroll;
        public GameObject ComparisonPanel;
        public TMP_Text ComparisonText;
        public UnityEngine.UI.Button CloseComparisonButton;
        public UnityEngine.UI.Toggle ReducedMotionToggle;
        public Material ArtworkMaterial;

        private Coroutine revealRoutine;
        private RenderTexture revealTexture;
        private Texture2D sourceTexture;
        private string artworkUrl = "";
        private string previousReply = "";
        private const float RevealSeconds = .9f;

        private void Awake()
        {
            foreach (var label in new[] { ReplyText, ArtworkTitle, ArtworkMetadata, ArtworkCollection,
                ArtworkRights, ComparisonText, StatusText, TimingsText, EngineNote })
                if (label != null) label.richText = false;
            if (CloseComparisonButton != null) CloseComparisonButton.onClick.AddListener(HideComparison);
            if (ReducedMotionToggle != null) ReducedMotionToggle.onValueChanged.AddListener(OnReducedMotionChanged);
            HideComparison();
        }

        private void LateUpdate()
        {
            if (ReplyText == null || ConversationScroll == null || ReplyText.text == previousReply) return;
            bool follow = string.IsNullOrEmpty(previousReply) || ConversationScroll.verticalNormalizedPosition <= .05f;
            previousReply = ReplyText.text;
            if (follow) ScrollToLatest();
        }

        public void ScrollToLatest()
        {
            if (ConversationScroll == null) return;
            Canvas.ForceUpdateCanvases();
            ConversationScroll.verticalNormalizedPosition = 0;
        }

        public void ShowComparison(string text)
        {
            if (ComparisonText != null) ComparisonText.text = text ?? "";
            if (ComparisonPanel != null) ComparisonPanel.SetActive(true);
            if (CloseComparisonButton != null) CloseComparisonButton.Select();
        }

        public void HideComparison()
        {
            if (ComparisonPanel != null) ComparisonPanel.SetActive(false);
        }

        public void SetArtwork(Texture2D texture, string title, string meta, string collection = "", string rights = "", string url = "")
        {
            StopReveal();
            sourceTexture = texture;
            if (ArtworkTitle != null) ArtworkTitle.text = title ?? "Archive artwork";
            if (ArtworkMetadata != null) ArtworkMetadata.text = meta ?? "";
            if (ArtworkCollection != null) ArtworkCollection.text = collection ?? "";
            if (ArtworkRights != null) ArtworkRights.text = rights ?? "";
            artworkUrl = PublicSource(url);
            if (SourceButton != null) SourceButton.gameObject.SetActive(artworkUrl.Length > 0);
            if (ArtworkPlaceholder != null) ArtworkPlaceholder.gameObject.SetActive(texture == null);
            if (ArtworkImage == null) return;
            ArtworkImage.enabled = texture != null;
            if (texture == null) { ApplyTexture(null); return; }

            var fitter = ArtworkImage.GetComponent<UnityEngine.UI.AspectRatioFitter>();
            if (fitter != null) fitter.aspectRatio = (float)texture.width / Mathf.Max(1, texture.height);
            if ((ReducedMotionToggle != null && ReducedMotionToggle.isOn) || !isActiveAndEnabled)
                ApplyTexture(texture);
            else revealRoutine = StartCoroutine(Reveal(texture));
        }

        public void ReplayReveal()
        {
            if (sourceTexture == null) return;
            StopReveal();
            if (ReducedMotionToggle != null && ReducedMotionToggle.isOn) ApplyTexture(sourceTexture);
            else if (isActiveAndEnabled) revealRoutine = StartCoroutine(Reveal(sourceTexture));
        }

        private IEnumerator Reveal(Texture2D texture)
        {
            float start = Time.unscaledTime;
            int previousSize = -1;
            while (Time.unscaledTime - start < RevealSeconds)
            {
                float progress = Mathf.Clamp01((Time.unscaledTime - start) / RevealSeconds);
                // Point-filtered GPU copies reveal the actual downloaded image.
                // No generated pixels, shader download or CPU ReadPixels is used.
                int longSide = Mathf.RoundToInt(Mathf.Lerp(18, Mathf.Min(768, Mathf.Max(texture.width, texture.height)), progress * progress));
                longSide = Mathf.Max(8, longSide / 8 * 8);
                if (longSide != previousSize)
                {
                    float scale = (float)longSide / Mathf.Max(texture.width, texture.height);
                    var next = RenderTexture.GetTemporary(Mathf.Max(1, Mathf.RoundToInt(texture.width * scale)),
                        Mathf.Max(1, Mathf.RoundToInt(texture.height * scale)), 0, RenderTextureFormat.ARGB32);
                    next.filterMode = FilterMode.Point;
                    next.wrapMode = TextureWrapMode.Clamp;
                    var previousActive = RenderTexture.active;
                    Graphics.Blit(texture, next);
                    RenderTexture.active = previousActive;
                    ApplyTexture(next);
                    if (revealTexture != null) RenderTexture.ReleaseTemporary(revealTexture);
                    revealTexture = next;
                    previousSize = longSide;
                }
                yield return null;
            }
            ApplyTexture(texture);
            ReleaseRevealTexture();
            revealRoutine = null;
        }

        private void ApplyTexture(Texture texture)
        {
            if (ArtworkImage != null) ArtworkImage.texture = texture;
            // If supplied, this is a controller-owned runtime material instance.
            if (ArtworkMaterial == null) return;
            if (ArtworkMaterial.HasProperty("_BaseMap")) ArtworkMaterial.SetTexture("_BaseMap", texture);
            if (ArtworkMaterial.HasProperty("_MainTex")) ArtworkMaterial.SetTexture("_MainTex", texture);
        }

        private void OnReducedMotionChanged(bool enabled)
        {
            if (!enabled || sourceTexture == null) return;
            StopReveal();
            ApplyTexture(sourceTexture);
        }

        private static string PublicSource(string value)
        {
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps
                || !string.IsNullOrEmpty(uri.UserInfo) || uri.IsLoopback || !uri.Host.Contains(".")) return "";
            return uri.AbsoluteUri;
        }

        private void StopReveal()
        {
            if (revealRoutine != null) { StopCoroutine(revealRoutine); revealRoutine = null; }
            if (ArtworkImage != null && ArtworkImage.texture == revealTexture) ApplyTexture(sourceTexture);
            ReleaseRevealTexture();
        }

        private void ReleaseRevealTexture()
        {
            if (revealTexture == null) return;
            RenderTexture.ReleaseTemporary(revealTexture);
            revealTexture = null;
        }

        private void OnDisable() { StopReveal(); }

        private void OnDestroy()
        {
            StopReveal();
            if (CloseComparisonButton != null) CloseComparisonButton.onClick.RemoveListener(HideComparison);
            if (ReducedMotionToggle != null) ReducedMotionToggle.onValueChanged.RemoveListener(OnReducedMotionChanged);
        }
    }
}
