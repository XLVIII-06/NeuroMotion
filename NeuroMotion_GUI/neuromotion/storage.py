"""Chunked metadata storage; decoded images never enter this store."""

from pathlib import Path
import numpy as np


CHUNK_ROWS = 4096


class RecordBuffer:
    def __init__(self, dtype: np.dtype, path: Path | None = None):
        self.dtype = dtype
        self.path = path
        self.pending: list[tuple] = []
        self.blocks: list[np.ndarray] = []
        self.stream = path.open("wb") if path else None

    def append(self, row: tuple) -> None:
        self.pending.append(row)
        if len(self.pending) >= CHUNK_ROWS:
            self.flush()

    def flush(self) -> None:
        if self.pending:
            block = np.asarray(self.pending, dtype=self.dtype)
            if self.stream:
                block.tofile(self.stream)
            else:
                self.blocks.append(block)
            self.pending.clear()

    def finish(self) -> np.ndarray:
        self.flush()
        self.close()
        if self.path:
            return np.memmap(self.path, dtype=self.dtype, mode="r+")
        return np.concatenate(self.blocks) if self.blocks else np.empty(0, dtype=self.dtype)

    def close(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None


def close_mapping(array: np.ndarray | None) -> None:
    if isinstance(array, np.memmap) and not array._mmap.closed:
        array.flush()
        array._mmap.close()
