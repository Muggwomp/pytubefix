from types import SimpleNamespace
from unittest.mock import Mock

from pytubefix.sabr.browser_stream import BrowserCapture, BrowserSabrStream, CaptureDeadline


def test_progress_can_continue_past_four_minutes(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('pytubefix.sabr.browser_stream.time.monotonic', lambda: clock[0])
    deadline = CaptureDeadline(240)
    for second in range(0, 1200, 30):
        clock[0] = second
        assert deadline.active(second * 1024)
    clock[0] += 241
    assert not deadline.active(deadline.downloaded)


def test_explicit_hard_limit_is_respected(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('pytubefix.sabr.browser_stream.time.monotonic', lambda: clock[0])
    deadline = CaptureDeadline(240, 60)
    clock[0] = 61
    assert not deadline.active(1000)


def test_gap_recovery_writes_ordered_bytes_once():
    capture = BrowserCapture(itag=134, end_segment_number=2)
    header = lambda seq: SimpleNamespace(sequenceNumber=seq, contentLength=1)
    try:
        capture.add_chunk(header(0), b'a')
        capture.add_chunk(header(2), b'c')
        assert capture.next_sequence == 1
        assert not capture.add_chunk(header(0), b'a')
        capture.mark_ended(0)
        assert 0 not in capture.chunks
        assert 0 not in capture.ended_sequences
        capture.add_chunk(header(1), b'b')
        assert capture.complete()
        assert b''.join(capture.iter_chunks()) == b'abc'
        assert capture.total_bytes == 3
    finally:
        capture.close()


def test_seek_uses_first_unfinished_segment_even_if_partial():
    backend = BrowserSabrStream.__new__(BrowserSabrStream)
    backend.capture = SimpleNamespace(end_segment_number=999, next_sequence=709,
                                     chunks={709: [b'partial']})
    backend.stream = SimpleNamespace(durationMs=1000000)
    page = Mock()
    backend._seek_to_missing_segment(page)
    assert page.locator.return_value.evaluate.call_args.args[1] == 708.0
