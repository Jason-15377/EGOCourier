"""Bounded, cancellable PCM diagnostics; silence is not proof of packet loss."""
import math

import numpy as np

from ego_checker.domain.audio import CorruptAudio


class PcmWaveform:
    BLOCK_FRAMES = 16_384
    MAX_ZERO_RUNS = 100

    def __init__(self, rate, channels, bits, codec=1, *, checkpoint=None):
        self.rate, self.channels, self.bits, self.codec = rate, channels, bits, codec
        self.checkpoint = checkpoint
        self.align = channels * bits // 8
        self.block_bytes = self.align * min(self.BLOCK_FRAMES, max(1, 1_048_576 // self.align))
        self.frames = self.zero_frames = self.invalid_samples = self.clipped_samples = 0
        self.finite_samples = 0
        self.sum_squares = self.peak = 0.0
        self.zero_start = None
        self.zero_start_record = None
        self.last_record = None
        self.zero_run_count = self.max_zero_frames = 0
        self.zero_runs = []
        self.previous = None
        self.constant_frames = self.max_constant_frames = 0
        self.finished = False

    def _finish_zero_run(self):
        if self.zero_start is None:
            return
        self._record_zero_run(self.frames, self.last_record)

    def _record_zero_run(self, end_frame, last_record):
        length = end_frame - self.zero_start
        self.max_zero_frames = max(self.max_zero_frames, length)
        if length >= self.rate * 2:
            self.zero_run_count += 1
            if len(self.zero_runs) < self.MAX_ZERO_RUNS:
                self.zero_runs.append(dict(
                    start_frame=self.zero_start, end_frame=end_frame,
                    first_record=self.zero_start_record, last_record=last_record))
        self.zero_start = None
        self.zero_start_record = None

    def _decode(self, block):
        sample_count = len(block) // (self.bits // 8)
        if self.bits == 24:
            octets = np.frombuffer(block, dtype=np.uint8).reshape(sample_count, 3)
            values = (octets[:, 0].astype(np.int32)
                      | octets[:, 1].astype(np.int32) << 8
                      | octets[:, 2].astype(np.int32) << 16)
            values = (values ^ 0x800000) - 0x800000
        elif self.bits == 8:
            values = np.frombuffer(block, dtype=np.uint8).astype(np.int16) - 128
        else:
            kind = 'f' if self.codec == 3 else 'i'
            values = np.frombuffer(block, dtype=f'<{kind}{self.bits // 8}')
        return values.reshape(-1, self.channels)

    def _update_zero_runs(self, zero_mask, source_record):
        count = int(np.count_nonzero(zero_mask))
        self.zero_frames += count
        if not zero_mask.size:
            return

        was_open = self.zero_start is not None
        previous_mask = np.empty(zero_mask.size, dtype=np.bool_)
        previous_mask[0] = was_open
        previous_mask[1:] = zero_mask[:-1]
        starts = np.flatnonzero(~previous_mask & zero_mask)
        ends = np.flatnonzero(previous_mask & ~zero_mask)
        base = self.frames

        prior_closed = was_open and bool(ends.size)
        if prior_closed:
            first_end = int(ends[0])
            last_record = self.last_record if first_end == 0 else source_record
            self._record_zero_run(base + first_end, last_record)
            ends = ends[1:]

        completed = min(starts.size, ends.size)
        if completed:
            completed_starts = starts[:completed]
            completed_ends = ends[:completed]
            lengths = completed_ends - completed_starts
            self.max_zero_frames = max(self.max_zero_frames, int(lengths.max()))
            qualifying = np.flatnonzero(lengths >= self.rate * 2)
            self.zero_run_count += int(qualifying.size)
            remaining = self.MAX_ZERO_RUNS - len(self.zero_runs)
            for index in qualifying[:remaining]:
                start = int(completed_starts[index])
                end = int(completed_ends[index])
                self.zero_runs.append(dict(
                    start_frame=base + start, end_frame=base + end,
                    first_record=source_record, last_record=source_record))

        if zero_mask[-1]:
            if not was_open or prior_closed:
                trailing_index = completed
                self.zero_start = base + int(starts[trailing_index])
                self.zero_start_record = source_record
            self.last_record = source_record
        else:
            self.zero_start = None
            self.zero_start_record = None

    def _update_constant_runs(self, values):
        frame_count = values.shape[0]
        if not frame_count:
            return
        changed = ((values[1:, 0] != values[:-1, 0])
                   | (values[1:, 1] != values[:-1, 1]) if self.channels == 2
                   else np.any(values[1:] != values[:-1], axis=1))
        changes = np.flatnonzero(changed) + 1
        continues = self.previous is not None and bool(np.all(values[0] == self.previous))
        starts = np.concatenate((np.array([0]), changes))
        ends = np.concatenate((changes, np.array([frame_count])))
        lengths = ends - starts
        if continues:
            lengths[0] += self.constant_frames
        self.max_constant_frames = max(self.max_constant_frames, int(lengths.max()))
        self.constant_frames = int(lengths[-1])
        self.previous = tuple(value.item() for value in values[-1])

    def _update_sample_statistics(self, values):
        scale = 1.0 if self.codec == 3 else float(1 << (self.bits - 1))
        normalized = values.astype(np.float64, copy=False) / scale
        if self.codec == 3:
            finite = np.isfinite(normalized)
            finite_values = (normalized.ravel() if bool(np.all(finite))
                             else normalized[finite])
        else:
            finite = None
            finite_values = normalized.ravel()
        invalid = normalized.size - finite_values.size
        self.invalid_samples += int(invalid)
        self.finite_samples += int(finite_values.size)
        if finite_values.size:
            self.peak = max(self.peak, float(np.max(np.abs(finite_values))))
            with np.errstate(over='ignore'):
                self.sum_squares += float(np.sum(
                    np.square(finite_values), dtype=np.float64))
        if self.codec == 3:
            clipped = finite & (np.abs(normalized) >= 1.0)
        else:
            minimum = -(1 << (self.bits - 1))
            maximum = (1 << (self.bits - 1)) - 1
            clipped = (values == minimum) | (values == maximum)
        self.clipped_samples += int(np.count_nonzero(clipped))

    def feed(self, data, *, source_record=None):
        if self.finished:
            raise RuntimeError('波形诊断已经结束')
        if len(data) % self.align:
            raise CorruptAudio('波形 PCM 数据不是完整采样帧')
        view = memoryview(data)
        for offset in range(0, len(view), self.block_bytes):
            if self.checkpoint:
                self.checkpoint()
            block = view[offset:offset + self.block_bytes]
            values = self._decode(block)
            zero_mask = ((values[:, 0] == 0) & (values[:, 1] == 0)
                         if self.channels == 2 else np.all(values == 0, axis=1))
            self._update_zero_runs(zero_mask, source_record)
            self._update_constant_runs(values)
            self._update_sample_statistics(values)
            self.frames += values.shape[0]
            self.last_record = source_record

    def finish(self, *, complete=True, data_offset=None):
        if not self.finished:
            self._finish_zero_run()
            self.finished = True
        return dict(
            complete=complete, frames=self.frames, zero_frames=self.zero_frames,
            zero_ratio=self.zero_frames / self.frames if self.frames else 0.0,
            peak=self.peak,
            rms=math.sqrt(self.sum_squares / self.finite_samples) if self.finite_samples else 0.0,
            invalid_samples=self.invalid_samples, clipped_samples=self.clipped_samples,
            max_zero_frames=self.max_zero_frames, max_constant_frames=self.max_constant_frames,
            zero_run_count=self.zero_run_count, zero_runs=self.zero_runs,
            zero_runs_omitted=self.zero_run_count - len(self.zero_runs),
            data_offset=data_offset, frame_bytes=self.align,
        )
