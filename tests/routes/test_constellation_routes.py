"""The constellation: who appears with whom (/api/reading-insights/constellation)."""
import math
from datetime import datetime, timezone

import pytest

from core.database import hide_issue_from_history, mark_issue_read
from models.reading_insights import ReadEvent, character_graph, layout_graph

AT = datetime(2024, 6, 10, 20, 0, tzinfo=timezone.utc)


def ev(n, characters=(), writers=(), pencillers=(), hidden=False):
    return ReadEvent(path=f"/data/x/{n}.cbz", at=AT, characters=tuple(characters),
                     writers=tuple(writers), pencillers=tuple(pencillers), hidden=hidden)


def names(graph):
    return [node["name"] for node in graph["nodes"]]


def edge_set(graph):
    by_id = {node["id"]: node["name"] for node in graph["nodes"]}
    return {(by_id[e["source"]], by_id[e["target"]], e["weight"]) for e in graph["edges"]}


class TestCharacterGraph:
    def test_nodes_are_sized_by_issues_and_ordered(self):
        graph = character_graph([
            ev(1, ["Batman", "Robin"]), ev(2, ["Batman", "Joker"]), ev(3, ["Batman"]),
        ])
        assert [(n["name"], n["count"]) for n in graph["nodes"]] == [
            ("Batman", 3), ("Joker", 1), ("Robin", 1)]
        assert [n["id"] for n in graph["nodes"]] == [0, 1, 2]
        assert {n["role"] for n in graph["nodes"]} == {"characters"}
        assert (graph["issues"], graph["total"]) == (3, 3)

    def test_an_edge_counts_shared_issues(self):
        graph = character_graph([
            ev(1, ["Batman", "Robin"]), ev(2, ["Robin", "Batman"]), ev(3, ["Batman", "Joker"]),
        ])
        assert edge_set(graph) == {("Batman", "Robin", 2), ("Batman", "Joker", 1)}

    def test_names_match_case_insensitively_and_keep_the_first_spelling(self):
        graph = character_graph([ev(1, ["Batman", "robin"]), ev(2, ["BATMAN", "Robin"])])
        assert names(graph) == ["Batman", "robin"]
        assert [n["count"] for n in graph["nodes"]] == [2, 2]

    def test_a_name_repeated_in_one_issue_counts_once(self):
        graph = character_graph([ev(1, ["Batman", "batman", "Robin"])])
        assert graph["nodes"][0]["count"] == 1
        assert len(graph["edges"]) == 1

    def test_only_the_most_frequent_are_kept(self):
        events = [ev(1, ["A", "B", "C"]), ev(2, ["A", "B"]), ev(3, ["A"])]
        graph = character_graph(events, limit=2)
        assert names(graph) == ["A", "B"]
        assert graph["total"] == 3
        # No edge points at a node that was cut.
        assert edge_set(graph) == {("A", "B", 2)}

    def test_each_node_keeps_only_its_strongest_ties(self):
        # Hub appears with S1 three times and with W1..W3 once each.
        events = [ev(i, ["Hub", "S1"]) for i in range(3)]
        events += [ev(10 + i, ["Hub", f"W{i}"]) for i in range(1, 4)]
        graph = character_graph(events, edges_per_node=1)
        # The hub nominates S1; each weak partner still nominates its own
        # only tie, so nobody is left unconnected.
        assert edge_set(graph) == {
            ("Hub", "S1", 3), ("Hub", "W1", 1), ("Hub", "W2", 1), ("Hub", "W3", 1)}

    def test_a_tie_between_two_busy_nodes_can_be_dropped(self):
        events = [ev(i, ["A", "B"]) for i in range(3)] + [ev(10 + i, ["C", "D"]) for i in range(3)]
        events += [ev(20, ["A", "C"])]
        graph = character_graph(events, edges_per_node=1)
        assert edge_set(graph) == {("A", "B", 3), ("C", "D", 3)}

    def test_hidden_reads_are_left_out(self):
        graph = character_graph([ev(1, ["Batman", "Robin"]), ev(2, ["Secret"], hidden=True)])
        assert names(graph) == ["Batman", "Robin"]
        assert graph["issues"] == 1

    def test_no_characters(self):
        assert character_graph([ev(1)]) == {"nodes": [], "edges": [], "issues": 0, "total": 0}

    def test_creators_join_writers_and_artists(self):
        graph = character_graph([
            ev(1, writers=["Tom King"], pencillers=["David Finch"]),
            ev(2, writers=["Tom King"], pencillers=["Mikel Janin"]),
        ], kind="creators")
        assert names(graph) == ["Tom King", "David Finch", "Mikel Janin"]
        assert {n["name"]: n["role"] for n in graph["nodes"]} == {
            "Tom King": "writer", "David Finch": "penciller", "Mikel Janin": "penciller"}
        assert edge_set(graph) == {("Tom King", "David Finch", 1), ("Tom King", "Mikel Janin", 1)}

    def test_a_writer_artist_is_one_node(self):
        graph = character_graph([ev(1, writers=["Jeff Lemire"], pencillers=["jeff lemire"])],
                                kind="creators")
        assert [(n["name"], n["count"], n["role"]) for n in graph["nodes"]] == [
            ("Jeff Lemire", 1, "writer")]
        assert graph["edges"] == []


class TestLayoutGraph:
    def test_trivial_sizes(self):
        assert layout_graph(0, []) == []
        assert layout_graph(1, []) == [(0.5, 0.5)]

    def test_is_deterministic(self):
        edges = [{"source": 0, "target": 1, "weight": 3}, {"source": 1, "target": 2, "weight": 1}]
        assert layout_graph(6, edges) == layout_graph(6, edges)

    def test_stays_inside_the_unit_square_with_a_margin(self):
        edges = [{"source": i, "target": (i * 7 + 3) % 30, "weight": 1 + i % 4}
                 for i in range(30) if i != (i * 7 + 3) % 30]
        positions = layout_graph(30, edges)
        assert len(positions) == 30
        assert all(0.05 <= x <= 0.95 and 0.05 <= y <= 0.95 for x, y in positions)

    def test_nodes_do_not_land_on_top_of_each_other(self):
        positions = layout_graph(40, [])
        closest = min(math.dist(a, b) for i, a in enumerate(positions) for b in positions[i + 1:])
        assert closest > 0.02

    def test_tied_nodes_end_up_closer_than_strangers(self):
        # Two tight triangles with nothing between them.
        edges = [{"source": a, "target": b, "weight": 5}
                 for a, b in ((0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5))]
        p = layout_graph(6, edges)
        within = max(math.dist(p[0], p[1]), math.dist(p[1], p[2]), math.dist(p[3], p[4]))
        across = min(math.dist(p[i], p[j]) for i in (0, 1, 2) for j in (3, 4, 5))
        assert within < across

    def test_two_nodes_fill_the_width(self):
        (ax, _), (bx, _) = layout_graph(2, [{"source": 0, "target": 1, "weight": 1}])
        assert abs(ax - bx) == pytest.approx(0.88, abs=0.01)


@pytest.fixture
def reads(db_connection):
    mark_issue_read("/data/DC/Batman/Batman 001.cbz", read_at="2024-06-01T10:00:00",
                    characters="Batman, Robin", writer="Tom King", penciller="David Finch")
    mark_issue_read("/data/DC/Batman/Batman 002.cbz", read_at="2024-06-02T10:00:00",
                    characters="Batman [123], Joker", writer="Tom King", penciller="Mikel Janin")
    mark_issue_read("/data/Image/Saga/Saga 001.cbz", read_at="2023-03-05T10:00:00",
                    characters="Alana, Marko", writer="Brian K. Vaughan")


class TestRoute:
    def test_characters_all_time(self, client, reads):
        body = client.get("/api/reading-insights/constellation").get_json()
        assert body["success"] is True
        assert (body["kind"], body["year"]) == ("characters", None)
        assert names(body)[0] == "Batman"
        assert set(names(body)) == {"Batman", "Robin", "Joker", "Alana", "Marko"}
        assert body["issues"] == 3
        assert all(0 <= n["x"] <= 1 and 0 <= n["y"] <= 1 for n in body["nodes"])
        assert ("Alana", "Marko", 1) in edge_set(body)

    def test_one_year(self, client, reads):
        body = client.get("/api/reading-insights/constellation?year=2023").get_json()
        assert set(names(body)) == {"Alana", "Marko"}
        assert body["year"] == 2023

    def test_creators(self, client, reads):
        body = client.get("/api/reading-insights/constellation?kind=creators").get_json()
        assert names(body)[0] == "Tom King"
        assert "Brian K. Vaughan" in names(body)

    def test_hidden_reads_are_left_out(self, client, reads):
        hide_issue_from_history("/data/Image/Saga/Saga 001.cbz")
        body = client.get("/api/reading-insights/constellation").get_json()
        assert "Alana" not in names(body)

    def test_a_new_read_is_not_hidden_by_the_cache(self, client, reads):
        assert "Storm" not in names(client.get("/api/reading-insights/constellation").get_json())
        # The reader's finish call is what clears the user's cached stats.
        client.post("/api/mark-comic-read", json={"path": "/data/Marvel/X-Men/X-Men 001.cbz"})
        mark_issue_read("/data/Marvel/X-Men/X-Men 001.cbz", characters="Storm, Cyclops")
        client.post("/api/mark-comic-read", json={"path": "/data/Marvel/X-Men/X-Men 002.cbz"})
        assert "Storm" in names(client.get("/api/reading-insights/constellation").get_json())

    def test_empty_history_is_not_cached_as_empty(self, client, db_connection):
        assert client.get("/api/reading-insights/constellation").get_json()["nodes"] == []
        mark_issue_read("/data/a.cbz", characters="Batman, Robin")
        assert len(client.get("/api/reading-insights/constellation").get_json()["nodes"]) == 2

    def test_per_user(self, client, reads):
        mark_issue_read("/data/other.cbz", characters="Hellboy", user_id=2)
        assert "Hellboy" not in names(client.get("/api/reading-insights/constellation").get_json())

    @pytest.mark.parametrize("query", ["kind=teams", "year=soon", "year=1500"])
    def test_bad_parameters(self, client, query):
        assert client.get(f"/api/reading-insights/constellation?{query}").status_code == 400
