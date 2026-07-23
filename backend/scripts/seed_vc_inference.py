from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import soundfile as sf
import torch
import torchaudio


def _save_pcm_wav(path: str, waveform: torch.Tensor, sample_rate: int, **_kwargs) -> None:
    """Evita a dependencia das DLLs compartilhadas do TorchCodec no Windows."""
    data = waveform.detach().cpu().numpy()
    if data.ndim == 2:
        data = data.T
    sf.write(path, data, sample_rate, subtype="PCM_16")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-vc-dir", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--diffusion-steps", type=int, default=30)
    parser.add_argument("--fp16", default="true")
    args = parser.parse_args()

    seed_vc_dir = Path(args.seed_vc_dir).resolve()
    sys.path.insert(0, str(seed_vc_dir))
    os.chdir(seed_vc_dir)
    torchaudio.save = _save_pcm_wav

    import inference

    inference.main(
        argparse.Namespace(
            source=args.source,
            target=args.target,
            output=args.output,
            diffusion_steps=args.diffusion_steps,
            length_adjust=1.0,
            inference_cfg_rate=0.7,
            f0_condition=False,
            auto_f0_adjust=False,
            semi_tone_shift=0,
            checkpoint=None,
            config=None,
            fp16=args.fp16.lower() == "true",
        )
    )


if __name__ == "__main__":
    main()
