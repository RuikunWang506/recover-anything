"""Closing a source twice must not close another file.

A Source holds a plain file number. Once closed, the system can hand that
number to the next file opened, and a second close used to close that file
instead (tests close every card image they opened at the end, whether or not
the test already did: conftest._close_card_images)."""
from recover_anything.source import Source


def test_closing_a_source_twice_leaves_other_files_open(tmp_path):
    card, other = tmp_path / "card.img", tmp_path / "other.txt"
    card.write_bytes(bytes(4096))
    other.write_bytes(b"still open")
    src = Source(str(card))
    src.close()
    with open(other, "rb") as f:  # (likely given the number the card had)
        src.close()
        assert f.read() == b"still open"
