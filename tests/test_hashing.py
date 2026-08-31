"""哈希与规范化测试（Level 1 确定性去重的基础）。"""

from trace.common.hashing import canonical_url, normalize_title, title_hash


def test_canonical_url_strips_tracking_params():
    a = canonical_url("https://example.com/news?id=42&utm_source=twitter&utm_medium=social")
    b = canonical_url("https://example.com/news?id=42")
    assert a == b


def test_canonical_url_case_and_trailing_slash():
    assert canonical_url("HTTPS://Example.com/Path/") == canonical_url("https://example.com/Path")


def test_title_normalization():
    assert normalize_title("BREAKING: Apple cuts NAND orders") == \
        normalize_title("apple cuts nand orders")
    assert title_hash("【路透】苹果调整 NAND 采购") != title_hash("三星扩产 HBM")
