# Bundled reference voices

These two reference recordings are excerpts from the **CSTR VCTK Corpus**, by Junichi Yamagishi, Christophe Veaux and Kirsten MacDonald, University of Edinburgh, Centre for Speech Technology Research.

Original dataset and citation: [VCTK version 0.92](https://doi.org/10.7488/ds/2645). The original corpus is licensed under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/); see `LICENSE-VCTK.txt`, including its disclaimer of warranties. The audio is not covered by this application's MIT license. No endorsement is implied.

- `female.wav`: speaker p225, utterance p225_011.
- `male.wav`: speaker p226, utterance p226_011.

Both were retrieved from the public [yfyeung/vctk evaluation mirror](https://huggingface.co/datasets/yfyeung/vctk), paired using `VCTK_10s.tsv` and `VCTK_10s.txt`. That mirror provides 10-second padded, 16 kHz clips. This application rewrites those WAVs as mono PCM16 at the same rate; no pitch or identity transformation was applied. Exact mirror URLs and transcripts are recorded in the corresponding `.source.json` files.

Speaker labels follow the VCTK metadata (p225 female, p226 male); they describe these two source recordings, not a claim that all women or men sound alike. The generated voice remains a model output. The reference transcript is the same for both clips:

> When a man looks for something beyond his reach, his friends say he is looking for the pot of gold at the end of the rainbow.

Use a different permitted reference for a different accent or vocal performance. The source recording license and Drama 3 model license are separate.
