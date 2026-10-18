from visdiary.render.cache import cache_key


def test_same_inputs_same_key(tmp_path):
    f = tmp_path / "ref.png"
    f.write_bytes(b"ref-bytes")
    k1 = cache_key("prompt", [f], "mock", "m0", "1:1", None)
    k2 = cache_key("prompt", [f], "mock", "m0", "1:1", None)
    assert k1 == k2


def test_any_change_changes_key(tmp_path):
    f = tmp_path / "ref.png"
    f.write_bytes(b"ref-bytes")
    base = cache_key("prompt", [f], "mock", "m0", "1:1", 7)
    variants = [
        cache_key("prompt!", [f], "mock", "m0", "1:1", 7),      # prompt 变
        cache_key("prompt", [f], "mock", "m0", "16:9", 7),      # aspect 变
        cache_key("prompt", [f], "mock", "m0", "1:1", 8),       # seed 变
        cache_key("prompt", [f], "real", "m0", "1:1", 7),       # provider 变
        cache_key("prompt", [f], "mock", "m1", "1:1", 7),       # model 变
        cache_key("prompt", [], "mock", "m0", "1:1", 7),        # 参考图变
    ]
    assert len({base, *variants}) == 7
    f.write_bytes(b"other-bytes")
    assert cache_key("prompt", [f], "mock", "m0", "1:1", 7) != base  # 参考图内容变
