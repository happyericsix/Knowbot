import json

from knowbot.envelope import Event


def test_seq_strictly_increasing():
    events = [Event("t.s", "t", {"i": i}) for i in range(200)]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 200


def test_payload_json_keeps_chinese():
    e = Event("audio.device", "snapshot", {"name": "耳机 (WH-1000XM5)"})
    row = e.to_row()
    assert json.loads(row[5])["name"] == "耳机 (WH-1000XM5)"
    # ensure_ascii=False：原始字节就是 UTF-8 中文
    assert "耳机" in row[5]


def test_to_json_from_json_roundtrip():
    e = Event("s", "t", {"a": 1})
    assert Event.from_json(e.to_json()) == e
