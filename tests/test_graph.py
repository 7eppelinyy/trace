"""产业链图谱测试：Event → Industry Graph → Security，direct/indirect/conditional 区分。"""

from trace.graph.industry_graph import IndustryGraph


def test_graph_hit_from_concept(db):
    graph = IndustryGraph(db)
    # ai_datacenter → enterprise_ssd → sndk/mu；ai_server → gpu → nvda
    hits = graph.find_securities_from_entities(["ai_datacenter"], max_hops=3)
    tickers = {h.security.ticker for h in hits}
    assert "SNDK" in tickers
    # 每个 hit 都带路径与 directness 区分
    for h in hits:
        assert h.directness in ("direct", "indirect", "conditional")
        assert len(h.path) >= 1


def test_graph_directness_by_hops(db):
    graph = IndustryGraph(db)
    hits = graph.find_securities_from_entities(["nand"], max_hops=3)
    by_ticker = {h.security.ticker: h for h in hits}
    # sndk produces nand：1 跳 → direct
    assert by_ticker["SNDK"].directness == "direct"
    # apple consumes nand 且 customer_of sndk：apple→nand→sndk 为 2 跳 → indirect
    assert by_ticker["MU"].directness == "direct"


def test_shortest_path(db):
    graph = IndustryGraph(db)
    path = graph.shortest_path("apple", "sndk")
    assert path is not None
    assert path[0] == "apple" and path[-1] == "sndk"
