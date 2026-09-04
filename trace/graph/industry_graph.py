"""产业链关系图。

Phase 1 不建设 Neo4j：使用 SQLite industry_edge + Python adjacency graph。

差异化能力核心：
    Event → Industry Graph → Security

即使原始新闻没有出现 SNDK，也可以通过
    Apple采购策略 → NAND → 存储竞争 → SNDK / MU
发现潜在关联。但必须区分 direct / indirect / conditional，
不得把二跳、三跳影响包装成确定直接影响。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from trace.db.connection import Database
from trace.db.repositories import IndustryEdgeRepo, SecurityRepo
from trace.domain.models import IndustryEdge, Security


@dataclass
class GraphHit:
    """从某个源节点出发，命中某个证券的结果。"""
    security: Security
    hops: int                          # 跳数：1=direct 关联，>=2=间接
    path: list[str] = field(default_factory=list)     # 节点路径
    edge_types: list[str] = field(default_factory=list)

    @property
    def directness(self) -> str:
        if self.hops <= 1:
            return "direct"
        if self.hops == 2:
            return "indirect"
        return "conditional"


class IndustryGraph:
    def __init__(self, db: Database):
        self.db = db
        self.edge_repo = IndustryEdgeRepo(db)
        self.security_repo = SecurityRepo(db)
        self._adj: dict[str, list[IndustryEdge]] = {}
        self._node_to_securities: dict[str, list[Security]] = {}
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._adj = {}
        for e in self.edge_repo.list_all():
            self._adj.setdefault(e.from_node, []).append(e)
            self._adj.setdefault(e.to_node, []).append(e)
        self._node_to_securities = {}
        for sec in self.security_repo.list_all():
            for node in sec.graph_node_ids or [sec.ticker.lower()]:
                self._node_to_securities.setdefault(node, []).append(sec)

    # ------------------------------------------------------------------
    def neighbors(self, node: str) -> list[IndustryEdge]:
        return self._adj.get(node, [])

    def securities_at(self, node: str) -> list[Security]:
        """挂在该图节点上的证券（预建倒排，O(1)）。

        调用方不得再用 "遍历全部 security 看 graph_node_ids 是否含 node"
        的写法：那在图遍历的内层循环里就是每节点一次全表扫描。
        """
        return self._node_to_securities.get(node.lower(), [])

    def neighbor_nodes(self, node: str) -> set[str]:
        """与该节点直接相邻的节点集合（无向）。"""
        return {e.to_node if e.from_node == node else e.from_node
                for e in self.neighbors(node)}

    # ------------------------------------------------------------------
    def find_securities_from_entities(self, entity_nodes: list[str],
                                      max_hops: int = 3) -> list[GraphHit]:
        """从事件涉及的实体节点出发做 BFS，找出可关联证券。

        返回去重后的命中列表（同一证券保留最短路径）。
        """
        best: dict[str, GraphHit] = {}
        for start in entity_nodes:
            start = start.lower()
            if start not in self._adj and start not in self._node_to_securities:
                continue
            self._bfs(start, max_hops, best)
        return sorted(best.values(), key=lambda h: (h.hops, h.security.ticker))

    def _bfs(self, start: str, max_hops: int, best: dict[str, GraphHit]) -> None:
        queue: deque[tuple[str, int, list[str], list[str]]] = deque()
        queue.append((start, 0, [start], []))
        visited = {start}
        while queue:
            node, hops, path, edge_types = queue.popleft()
            # 当前节点本身可能直接对应证券
            for sec in self._node_to_securities.get(node, []):
                hit = GraphHit(security=sec, hops=max(hops, 1),
                               path=list(path), edge_types=list(edge_types))
                self._record(best, hit)
            if hops >= max_hops:
                continue
            for edge in self.neighbors(node):
                nxt = edge.to_node if edge.from_node == node else edge.from_node
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append((nxt, hops + 1, path + [nxt], edge_types + [edge.edge_type]))

    @staticmethod
    def _record(best: dict[str, GraphHit], hit: GraphHit) -> None:
        key = hit.security.security_id
        if key not in best or hit.hops < best[key].hops:
            best[key] = hit

    # ------------------------------------------------------------------
    def shortest_path(self, from_node: str, to_node: str,
                      max_hops: int = 4) -> list[str] | None:
        """返回两节点间的最短路径（用于 /ask 解释与产业传导展示）。"""
        from_node, to_node = from_node.lower(), to_node.lower()
        if from_node == to_node:
            return [from_node]
        queue: deque[tuple[str, list[str]]] = deque([(from_node, [from_node])])
        visited = {from_node}
        while queue:
            node, path = queue.popleft()
            if len(path) - 1 >= max_hops:
                continue
            for edge in self.neighbors(node):
                nxt = edge.to_node if edge.from_node == node else edge.from_node
                if nxt in visited:
                    continue
                new_path = path + [nxt]
                if nxt == to_node:
                    return new_path
                visited.add(nxt)
                queue.append((nxt, new_path))
        return None
