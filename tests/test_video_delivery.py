"""The submitted MP4 relocation must preserve media bytes and chunk offsets."""
import struct
import unittest
from finalize_competition_video import faststart


def box(kind, body):
    return struct.pack('>I4s', len(body) + 8, kind) + body


class VideoDeliveryTests(unittest.TestCase):
    def test_late_movie_metadata_preserves_payload_and_adjusts_both_offset_widths(self):
        for kind, fmt in [(b'stco', '>I'), (b'co64', '>Q')]:
            with self.subTest(kind=kind):
                header = box(b'ftyp', b'isom0000')
                payload = b'ORIGINAL_MEDIA_BYTES'
                offset = len(header) + 8
                table = box(kind, b'\0' * 4 + struct.pack('>I', 1) + struct.pack(fmt, offset))
                movie = box(b'moov', box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', table)))))
                original = header + box(b'mdat', payload) + movie
                result = faststart(original)
                self.assertEqual(len(result), len(original))
                self.assertEqual(result[len(header) + 4:len(header) + 8], b'moov')
                start = result.index(kind) + 12
                new_offset = struct.unpack(fmt, result[start:start + struct.calcsize(fmt)])[0]
                self.assertEqual(new_offset, offset + len(movie))
                self.assertEqual(result[new_offset:new_offset + len(payload)], payload)

    def test_existing_faststart_bytes_are_unchanged(self):
        source = box(b'ftyp', b'isom0000') + box(b'moov', b'') + box(b'mdat', b'data')
        self.assertEqual(faststart(source), source)

    def test_truncated_and_fragmented_inputs_are_rejected(self):
        for source in [b'\0\0\0\x20moov', box(b'ftyp', b'isom') + box(b'moof', b'')]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                faststart(source)
