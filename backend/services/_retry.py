from __future__ import annotations

import gc
import time
from typing import Callable, TypeVar

import torch

T = TypeVar("T")

# Sem pagefile configurado no Windows, algumas alocacoes (inclusive pequenas) podem
# falhar de forma transiente quando o commit charge do sistema (nao so deste processo)
# esta perto do teto -- um retry com gc/empty_cache costuma passar na proxima tentativa.
MAX_RETRIES = 3
RETRY_DELAY_S = 5


def with_retry(fn: Callable[[], T]) -> T:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except (MemoryError, OSError, torch.OutOfMemoryError) as exc:
            last_error = exc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            time.sleep(RETRY_DELAY_S)
    raise last_error
