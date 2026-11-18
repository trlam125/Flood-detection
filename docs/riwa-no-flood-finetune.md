# RIWA no-flood fine-tune

The experimental checkpoint, `models/riwa_no_flood_finetuned.pth`, was fine-tuned
from `models/riwa_unet_resnet34_deploy.pth`. The original checkpoint was left
unchanged.

The run sampled 72 evenly spaced frames from each of the three supplied videos
(216 frames total). It held out the final 20% of sampled frames from every clip,
froze the ResNet34 encoder, and trained decoder parameters for four epochs with
AdamW at a learning rate of `1e-5`. All target masks represented no water.

On the held-out dry frames, the fraction of pixels classified as water at the
original RIWA threshold decreased from 32.4643% before fine-tuning to 1.6276%
after fine-tuning. This measures false positives on these three clips only; it
does not measure flood recall or performance on other cameras and conditions.

To reproduce the fine-tune, run `python scripts/finetune_riwa.py` with the source
videos at the paths configured at the top of that script. It writes a separate
checkpoint only if the held-out dry false-positive area improves. The script
never overwrites the original deployment checkpoint.
