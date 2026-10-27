"""The read-only graph viewer: routes, record reading, escaping and the loopback server.

The fixture project ``fixtures/viewer/project`` holds one item with four runs
written through ``llm_kit.graph.records.RunStore``: a seed, a failed run, a run
stopped at its human node and a run still running. Tests that change records
work on a copy.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from llm_kit.graph.commands import serve as serve_command
from llm_kit.graph.config import load_config
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import GraphError
from llm_kit.graph.records import RunStore
from llm_kit.graph.viewer import runs as runs_module
from llm_kit.graph.viewer import server as server_module
from llm_kit.graph.viewer.server import STATIC_FILES, ViewerApp, make_server
from llm_kit.graph.viewer.structure import embedded_structure, graph_structure, map_path, unmapped_paths

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "viewer" / "project"
STATIC = Path(server_module.__file__).resolve().parent / "static"
PORT = 4321
HOST = {"Host": f"127.0.0.1:{PORT}"}

SEED = "20261026T090000000000Z-0005"
FAILED = "20261026T100000000000Z-a001"
STOPPED = "20261026T110000000000Z-b002"
RUNNING = "20261026T120000000000Z-c003"
ITEM = "night-petition"


def make_app(project: Path = FIXTURE) -> ViewerApp:
    return ViewerApp(load_config(project), port=PORT)


@pytest.fixture(scope="module")
def app() -> ViewerApp:
    return make_app()


def get(app: ViewerApp, target: str, headers=HOST):
    return app.handle("GET", target, headers)


def get_json(app: ViewerApp, target: str):
    response = get(app, target)
    assert response.status == 200, response.body[:300]
    assert response.header("Content-Type").startswith("application/json")
    return json.loads(response.body)


def run_view(app: ViewerApp, run_id: str):
    return get_json(app, f"/api/run/{ITEM}/{run_id}")


def detail(app: ViewerApp, run_id: str, path: str):
    from urllib.parse import quote

    return get_json(app, f"/api/run/{ITEM}/{run_id}/node?path={quote(path, safe='')}")


@pytest.fixture
def project(tmp_path) -> Path:
    copy = tmp_path / "project"
    shutil.copytree(FIXTURE, copy)
    return copy


def rewrite_run(project: Path, run_id: str, edit) -> RunStore:
    store = RunStore.for_item(load_config(project), ITEM)
    record = store.load_run(run_id)
    edit(record, store)
    store.write_run(record)
    return store


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_a_graph_file_becomes_json_with_edges_and_bodies(self):
        graph = load_graph(FIXTURE / "graphs" / "night.graph.yaml")
        tree = graph_structure(graph, root=FIXTURE)
        json.dumps(tree)
        nodes = {n["id"]: n for n in tree["nodes"]}
        assert tree["file"] == "graphs/night.graph.yaml" and tree["outer"] is True
        assert list(nodes) == ["brief", "plot", "poses", "bundle", "approve", "encode"]
        assert nodes["plot"]["deps"] == ["brief"]
        assert nodes["poses"]["deps"] == ["plot"]
        assert nodes["bundle"]["deps"] == ["plot", "poses"]
        assert nodes["encode"]["deps"] == ["approve", "bundle"]
        assert nodes["brief"]["deps"] == []  # a prompt path and a model key are not edges
        assert nodes["brief"]["subtitle"] == "llm.model - demo.brief.write - high"
        loop = {n["id"]: n for n in nodes["plot"]["body"]["nodes"]}["loop"]
        assert loop["kind"] == "repeat" and loop["detail"]["until"] == "round.score >= 9"
        body = {n["id"]: n for n in loop["body"]["nodes"]}
        assert loop["body"]["inline"] is True
        assert body["repair"]["deps"] == ["check", "write"] and body["repair"]["when"] == "not check.out.ok"
        assert body["evaluate"]["deps"] == ["write"]
        assert nodes["poses"]["detail"] == {"over": "plot.out.plot.poses", "key": "item.id"}

    def test_the_run_record_table_gives_the_same_shape(self):
        store = RunStore.for_item(load_config(FIXTURE), ITEM)
        record = store.load_run(FAILED)
        tree = embedded_structure(record["graphs"], record["graph"]["hash"])
        assert {n["id"] for n in tree["nodes"]} == {"brief", "plot", "poses", "bundle", "approve", "encode"}
        plot = {n["id"]: n for n in tree["nodes"]}["plot"]
        assert plot["body"] is not None and {n["id"] for n in plot["body"]["nodes"]} == {"loop", "keep"}

    def test_a_cycle_or_a_missing_graph_in_the_table_is_an_error(self):
        with pytest.raises(GraphError, match="no structure"):
            embedded_structure({}, "a" * 64)
        table = {"a": {"id": "x", "nodes": {"n": {"kind": "graph", "deps": [], "body": "a"}}}}
        with pytest.raises(GraphError, match="cycle"):
            embedded_structure(table, "a")

    @pytest.mark.parametrize("path, expected", [
        ("brief", True),
        ("plot/loop", True),
        ("plot/loop[2]", True),
        ("plot/loop[2]/write", True),
        ("poses[p-3]/render", True),
        ("poses[p%2F3]/render", True),
        ("brief[1]", False),            # a key on a node that is not a map or repeat
        ("poses/render", False),        # a map body needs its item key
        ("plot/nothing", False),
        ("nothing", False),
        ("brief/child", False),
        ("plot/loop[1]/write/x", False),
    ])
    def test_record_paths_map_onto_the_structure(self, path, expected):
        store = RunStore.for_item(load_config(FIXTURE), ITEM)
        record = store.load_run(FAILED)
        tree = embedded_structure(record["graphs"], record["graph"]["hash"])
        assert map_path(tree, path.split("/")) is expected
        assert unmapped_paths(tree, [path]) == ([] if expected else [path])


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


class TestPageAndProjectFiles:
    def test_the_page_and_its_files(self, app):
        page = get(app, "/")
        assert page.status == 200 and page.header("Content-Type").startswith("text/html")
        for name, content_type in STATIC_FILES.items():
            response = get(app, f"/static/{name}")
            assert response.status == 200 and response.header("Content-Type") == content_type, name
            assert response.body == (STATIC / name).read_bytes()

    def test_every_file_of_static_is_listed_and_exists(self):
        assert {p.name for p in STATIC.iterdir()} == set(STATIC_FILES)

    def test_the_page_names_only_files_it_serves(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        refs = set(re.findall(r'(?:src|href)="([^"]+)"', html))
        assert refs == {"/static/viewer.css", "/theme.css", "/static/app.js"}
        for name in re.findall(r'from "\./([a-z]+\.js)"', "".join((STATIC / n).read_text() for n in STATIC_FILES if n.endswith(".js"))):
            assert name in STATIC_FILES

    @pytest.mark.parametrize("target", [
        "/static/server.py", "/static/..%2Fserver.py", "/static/../server.py", "/static/", "/static/index.html/x",
        "/nothing", "/api", "/api/nothing", "/panels/1.mjs", "/panels/x.mjs", "/panels/../x", "/media/a", "/api/run/x",
    ])
    def test_unknown_routes_are_404(self, app, target):
        assert get(app, target).status == 404

    def test_the_project_theme_and_panel_are_served_from_the_config(self, app):
        theme = get(app, "/theme.css")
        assert theme.header("Content-Type").startswith("text/css")
        assert theme.body == (FIXTURE / "viewer" / "theme.css").read_bytes()
        panel = get(app, "/panels/0.mjs")
        assert panel.header("Content-Type").startswith("text/javascript")
        assert panel.body == (FIXTURE / "viewer" / "panels.mjs").read_bytes()
        session = get_json(app, "/api/session")
        assert session["panels"] == ["/project/viewer/panels.mjs"] and session["theme"] == "/project/viewer/theme.css"
        assert get(app, session["panels"][0]).body == panel.body and get(app, session["theme"]).body == theme.body

    def test_a_panel_outside_the_project_is_served_by_number(self, project, tmp_path):
        outside = tmp_path / "elsewhere.mjs"
        outside.write_text("export default [];", encoding="utf-8")
        config = project / ".llm-kit" / "graphs.local.yaml"
        config.write_text(f"version: 1\nviewer:\n  panels: [{outside}]\n", encoding="utf-8")
        app = make_app(project)
        assert get_json(app, "/api/session")["panels"] == ["/panels/0.mjs"]
        assert get(app, "/panels/0.mjs").body == b"export default [];"

    def test_a_panel_may_import_a_neighbour_through_the_project_route(self, project):
        (project / "viewer" / "helper.mjs").write_text("export const x = 1;", encoding="utf-8")
        (project / "other").mkdir()
        (project / "other" / "render.js").write_text("export const y = 2;", encoding="utf-8")
        app = make_app(project)
        assert get(app, "/project/viewer/helper.mjs").body == b"export const x = 1;"
        # what a relative import from /project/viewer/panels.mjs resolves to
        assert get(app, "/project/other/render.js").header("Content-Type").startswith("text/javascript")

    def test_the_project_route_serves_json_as_json(self, project):
        (project / "viewer" / "words.json").write_text('{"a": 1}', encoding="utf-8")
        response = get(make_app(project), "/project/viewer/words.json")
        assert response.status == 200 and response.body == b'{"a": 1}'
        assert response.header("Content-Type").startswith("application/json")

    @pytest.mark.parametrize("target", [
        "/project/..%2F..%2Fetc%2Fpasswd.json", "/project/viewer/..%2F..%2Fx.json", "/project/.llm-kit/x.json",
        "/project/viewer/.hidden.json", "/project/viewer/missing.json",
    ])
    def test_json_under_the_project_route_keeps_the_guards(self, app, target):
        assert get(app, target).status == 404

    def test_a_json_link_out_of_the_project_is_not_followed(self, project, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text("{}", encoding="utf-8")
        (project / "viewer" / "link.json").symlink_to(secret)
        assert get(make_app(project), "/project/viewer/link.json").status == 404

    @pytest.mark.parametrize("target", [
        "/project/.llm-kit/graphs.yaml", "/project/graphs/night.graph.yaml", "/project/work/night-petition/graph/runs/x.py",
        "/project/..%2F..%2Fetc%2Fpasswd", "/project/../test_viewer.py", "/project/viewer/..%2F..%2Fx.mjs",
        "/project/.git/config.js", "/project/viewer/.hidden.mjs", "/project/viewer", "/project/", "/project/viewer/missing.mjs",
        "/project/viewer/panels.mjs%00.png",
    ])
    def test_the_project_route_serves_only_scripts_styles_and_json_inside_the_project(self, app, target):
        assert get(app, target).status == 404

    def test_a_link_out_of_the_project_is_not_followed(self, project, tmp_path):
        secret = tmp_path / "secret.js"
        secret.write_text("export const secret = 1;", encoding="utf-8")
        (project / "viewer" / "link.js").symlink_to(secret)
        assert get(make_app(project), "/project/viewer/link.js").status == 404

    def test_without_a_theme_the_theme_route_is_empty_css(self, project):
        config = project / ".llm-kit" / "graphs.yaml"
        config.write_text(config.read_text().replace("viewer:\n  panels: [viewer/panels.mjs]\n  theme: viewer/theme.css\n", ""))
        bare = make_app(project)
        assert get(bare, "/theme.css").status == 200
        assert get_json(bare, "/api/session")["theme"] is None and get_json(bare, "/api/session")["panels"] == []

    def test_a_configured_file_that_is_missing_fails_at_start(self, project):
        (project / "viewer" / "theme.css").unlink()
        with pytest.raises(GraphError, match="theme.css does not exist"):
            make_app(project)

    def test_the_session(self, app):
        session = get_json(app, "/api/session")
        assert session["graphs"] == ["demo.night"] and session["runs_configured"] is True
        assert session["writable"] is False and "token" not in session

    def test_a_graph_by_name(self, app):
        tree = get_json(app, "/api/graph/demo.night")
        assert tree["name"] == "demo.night" and tree["structure"]["id"] == "demo.night"
        assert get(app, "/api/graph/other").status == 404


class TestSecurityHeaders:
    @pytest.mark.parametrize("target", ["/", "/static/app.js", "/api/session", "/api/runs", "/nothing"])
    def test_every_response_is_locked_down(self, app, target):
        response = get(app, target)
        assert response.header("X-Content-Type-Options") == "nosniff"
        assert response.header("Cache-Control") == "no-store"
        csp = response.header("Content-Security-Policy")
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp
        assert "'unsafe-eval'" not in csp and "frame-ancestors 'none'" in csp

    @pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "localhost", "", "127.0.0.1:4321.evil.example"])
    def test_a_foreign_host_is_refused(self, app, host):
        assert app.handle("GET", "/api/session", {"Host": host}).status == 403

    @pytest.mark.parametrize("host", ["127.0.0.1:4321", "localhost:4321", "[::1]:4321"])
    def test_the_loopback_names_are_accepted(self, app, host):
        assert app.handle("GET", "/api/session", {"Host": host}).status == 200

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def test_every_write_method_is_refused(self, app, method):
        response = app.handle(method, "/api/session", {**HOST, "Origin": f"http://127.0.0.1:{PORT}"}, b"{}")
        assert response.status == 405 and response.header("Allow") == "GET, HEAD"

    def test_head_has_headers_and_no_body(self, app):
        response = app.handle("HEAD", "/api/session", HOST)
        assert response.status == 200 and response.body == b"" and int(response.header("Content-Length")) > 0


class TestRuns:
    def test_every_run_and_seed_is_listed_newest_first(self, app):
        listing = get_json(app, "/api/runs")
        assert listing["configured"] is True and listing["error"] is None
        runs = listing["runs"]
        assert [r["run_id"] for r in runs] == [RUNNING, STOPPED, FAILED, SEED]
        by_id = {r["run_id"]: r for r in runs}
        assert by_id[FAILED]["status"] == "failed" and by_id[FAILED]["kind"] == "run"
        assert by_id[SEED]["kind"] == "seed" and by_id[SEED]["cost_usd"] == 0
        assert by_id[FAILED]["graph"] == "demo.night"
        assert by_id[FAILED]["counts"]["gated"] == 4 and by_id[FAILED]["cost_usd"] == pytest.approx(0.19)

    def test_the_failed_run_view(self, app):
        view = run_view(app, FAILED)
        assert view["status"] == "failed" and view["graph"] == "demo.night" and view["unmapped"] == []
        nodes = view["nodes"]
        assert nodes["brief"]["status"] == "done" and nodes["brief"]["model"] == "deepseek/deepseek-v4-flash-0731"
        assert nodes["brief"]["effort"] == "high" and nodes["brief"]["cost_usd"] == pytest.approx(0.0012)
        assert nodes["poses[p3]/render"]["status"] == "failed"
        assert nodes["poses[p3]/render"]["error"].startswith("OpFailed: image review refused")
        assert nodes["poses[p3]/review"]["status"] == "gated" and nodes["bundle"]["status"] == "gated"
        assert nodes["plot/loop[1]/repair"]["status"] == "skipped"
        assert nodes["poses[p2]/render"]["status"] == "reused" and nodes["poses[p2]/render"]["media"] == 2
        assert nodes["brief"]["duration_s"] == 8
        assert {n["id"] for n in view["structure"]["nodes"]} == {"brief", "plot", "poses", "bundle", "approve", "encode"}
        assert [nodes[f"poses[p{n}]"]["index"] for n in (1, 2, 3)] == [0, 1, 2]
        assert view["cost_usd"] == pytest.approx(0.19)

    def test_round_scores_come_from_the_repeat_record(self, app):
        nodes = run_view(app, FAILED)["nodes"]
        assert [nodes[f"plot/loop[{n}]"]["score"] for n in range(3)] == [6.0, 8.5, 7.5]
        assert nodes["plot/loop[0]/write"]["score"] is None

    def test_a_stopped_run_reports_why(self, app):
        view = run_view(app, STOPPED)
        assert view["status"] == "stopped" and view["stop"]["node"] == "approve"
        assert view["nodes"]["approve"]["status"] == "stopped" and view["nodes"]["encode"]["status"] == "pending"
        assert view["nodes"]["brief"]["status"] == "seeded" and view["nodes"]["plot"]["status"] == "reused"

    def test_a_running_run_shows_the_nodes_that_must_be_running(self, app):
        nodes = run_view(app, RUNNING)["nodes"]
        assert nodes["brief"]["status"] == "done"
        assert nodes["plot"]["status"] == "running" and nodes["plot"]["synthetic"] is True
        assert nodes["plot/loop[0]/write"]["status"] == "done"
        derived = nodes["plot/loop[0]/check"]
        assert derived["status"] == "running" and derived["derived"] is True
        assert "poses" not in nodes and "bundle" not in nodes   # they wait for plot

    def test_a_finished_run_derives_nothing(self, app):
        view = run_view(app, FAILED)
        assert not any(n.get("derived") for n in view["nodes"].values())

    def test_the_header_says_when_the_file_last_changed(self, app):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", run_view(app, RUNNING)["updated"])

    def test_an_unknown_run_or_item_is_404(self, app):
        assert get(app, f"/api/run/{ITEM}/20260101T000000000000Z-0000").status == 404
        assert get(app, f"/api/run/{ITEM}/not-a-run").status == 404
        assert get(app, f"/api/run/nobody/{FAILED}").status == 404
        assert get(app, f"/api/run/..%2F..%2Fx/{FAILED}").status == 404

    def test_records_that_match_no_node_are_listed(self, project):
        def edit(record, store):
            record["nodes"]["ghost/node"] = dict(record["nodes"]["brief"], path="ghost/node")

        rewrite_run(project, FAILED, edit)
        view = run_view(make_app(project), FAILED)
        assert view["unmapped"] == ["ghost/node"] and "ghost/node" in view["nodes"]


class TestNodeDetail:
    def test_a_model_node_shows_the_exact_prompt_and_answer(self, app):
        d = detail(app, FAILED, "brief")
        assert d["status"] == "done" and d["kind"] == "op" and d["op"] == "llm.model@1"
        assert d["attributes"]["sent"].startswith("You are writing a brief for a Moment.")
        assert json.loads(d["attributes"]["text"]) == d["out"]
        assert d["out"]["title"] == "The Petition"
        assert d["inputs"] == {"seed": {"name": "night-petition"}}
        assert d["params"]["model"] == "demo.brief.write"
        assert d["facts"] == {"model": "deepseek/deepseek-v4-flash-0731", "effort": "high", "cost_usd": 0.0012,
                              "usage": {"input_tokens": 900, "output_tokens": 320, "wall_ms": 7900}}
        assert d["receipt"]["usage"]["input_tokens"] == 900
        assert len(d["definition_hash"]) == 64 and d["duration_s"] == 8

    def test_a_failed_node_carries_its_error(self, app):
        d = detail(app, FAILED, "poses[p3]/render")
        assert d["status"] == "failed" and d["error"]["type"] == "OpFailed"
        assert "second face" in d["error"]["message"]

    def test_a_reused_node_names_its_source(self, app):
        assert detail(app, FAILED, "poses[p2]/render")["reused_from"]["path"] == "poses[p2]/render"

    def test_a_seeded_node_names_its_source(self, app):
        d = detail(app, SEED, "brief")
        assert d["status"] == "seeded" and d["seed"] == {"source": {"path": "work/night-petition/brief.json"}, "valid": True}

    def test_a_round_record_has_its_carry(self, app):
        d = detail(app, FAILED, "plot/loop[1]")
        assert d["kind"] == "round" and d["index"] == 1 and d["carry"] == {"best": 8.5}

    def test_media_get_a_url_and_a_type(self, app):
        d = detail(app, FAILED, "poses[p2]/render")
        kinds = sorted(m["mime"] for m in d["media"])
        assert kinds == ["audio/wav", "image/png"]
        for entry in d["media"]:
            assert entry["url"].startswith(f"/media/{ITEM}/")
            served = get(app, entry["url"])
            assert served.status == 200 and served.header("Content-Type") == entry["mime"]

    def test_a_node_without_a_record_of_its_own_lists_its_children(self, app):
        d = detail(app, RUNNING, "plot")
        assert d["synthetic"] is True and d["status"] == "running"
        assert d["children"] == ["plot/loop", "plot/loop[0]"]
        poses = detail(app, FAILED, "poses")
        assert poses["synthetic"] is False

    def test_an_unknown_node_is_404_and_a_path_is_required(self, app):
        assert get(app, f"/api/run/{ITEM}/{FAILED}/node?path=nope").status == 404
        assert get(app, f"/api/run/{ITEM}/{FAILED}/node").status == 400
        assert get(app, f"/api/run/{ITEM}/{FAILED}/node?path=a&path=b").status == 400

    def test_media_names_are_checked(self, app):
        name = detail(app, FAILED, "poses[p1]/render")["media"][0]["name"]
        assert get(app, f"/media/{ITEM}/{name}").status == 200
        for bad in ("../../.llm-kit/graphs.yaml", "..%2Fgraphs.yaml", "0000000000000000.png", "x.png", name.upper()):
            assert get(app, f"/media/{ITEM}/{bad}").status == 404
        assert get(app, f"/media/nobody/{name}").status == 404

    def test_an_unknown_media_type_is_a_download(self, project):
        media = project / "work" / ITEM / "media"
        (media / "0123456789abcdef.svg").write_text("<svg onload=alert(1)>", encoding="utf-8")
        response = get(make_app(project), f"/media/{ITEM}/0123456789abcdef.svg")
        assert response.status == 200 and response.header("Content-Type") == "application/octet-stream"
        assert "attachment" in response.header("Content-Disposition")


class TestStoredValues:
    def test_the_whole_text_of_a_reference_is_served_as_plain_text(self, app):
        ref = RunStore.for_item(load_config(FIXTURE), ITEM).load_run(FAILED)["nodes"]["brief"]["attributes"]["sent"]
        response = get(app, f"/api/ref/{ITEM}/{ref}")
        assert response.status == 200 and response.header("Content-Type") == "text/plain; charset=utf-8"
        assert response.body.decode().startswith("You are writing a brief")
        assert get(app, f"/api/ref/{ITEM}/blob:{'0' * 64}").status == 404
        assert get(app, f"/api/ref/{ITEM}/media:0123456789abcdef.png").status == 404
        assert get(app, f"/api/ref/{ITEM}/blob:nothex").status == 404

    def test_a_very_large_value_is_cut_to_a_preview_with_a_link(self, project):
        big = "x" * (runs_module.INLINE_LIMIT + 10)

        def edit(record, store):
            record["nodes"]["brief"]["attributes"]["text"] = store.put_blob(big)

        rewrite_run(project, FAILED, edit)
        app = make_app(project)
        text = detail(app, FAILED, "brief")["attributes"]["text"]
        assert text["$truncated"] is True and text["size"] == len(big) and len(text["preview"]) == runs_module.PREVIEW_CHARS
        assert get(app, f"/api/ref/{ITEM}/{text['ref']}").body.decode() == big

    def test_a_missing_store_object_is_reported_in_place(self, project):
        ref = RunStore.for_item(load_config(project), ITEM).load_run(FAILED)["nodes"]["brief"]["attributes"]["sent"]
        digest = ref.removeprefix("blob:")
        (project / "state" / "blobs" / digest[:2] / digest).unlink()
        d = detail(make_app(project), FAILED, "brief")
        assert "missing" in d["attributes"]["sent"]["$error"] and d["attributes"]["sent"]["ref"] == ref
        assert d["out"]["title"] == "The Petition"   # the rest still shows

    def test_a_corrupt_store_object_is_reported_in_place(self, project):
        ref = RunStore.for_item(load_config(project), ITEM).load_run(FAILED)["nodes"]["brief"]["attributes"]["sent"]
        digest = ref.removeprefix("blob:")
        (project / "state" / "blobs" / digest[:2] / digest).write_text("changed", encoding="utf-8")
        assert "does not match its hash" in detail(make_app(project), FAILED, "brief")["attributes"]["sent"]["$error"]


class TestUnusableRecords:
    def test_an_unreadable_run_is_listed_with_its_error_and_the_rest_still_show(self, project):
        runs_dir = project / "work" / ITEM / "graph" / "runs"
        (runs_dir / "20261026T130000000000Z-dead.json").write_text(json.dumps({"format": "other"}), encoding="ascii")
        app = make_app(project)
        runs = get_json(app, "/api/runs")["runs"]
        bad = [r for r in runs if r["status"] == "unreadable"]
        assert len(bad) == 1 and "not in format" in bad[0]["error"] and bad[0]["run_id"].endswith("-dead")
        assert len(runs) == 5
        assert get(app, f"/api/run/{ITEM}/20261026T130000000000Z-dead").status == 422

    def test_a_record_without_graph_structure_is_a_clear_error(self, project):
        rewrite_run(project, FAILED, lambda record, store: record.update(graphs={}))
        response = get(make_app(project), f"/api/run/{ITEM}/{FAILED}")
        assert response.status == 422 and b"no structure" in response.body

    def test_a_project_without_runs_dir_lists_graphs_only(self, project):
        config = project / ".llm-kit" / "graphs.yaml"
        config.write_text(re.sub(r"runs:\n(  .*\n)+", "", config.read_text()))
        app = make_app(project)
        listing = get_json(app, "/api/runs")
        assert listing["configured"] is False and listing["runs"] == [] and "no runs.dir" in listing["note"]
        assert get_json(app, "/api/session")["runs_configured"] is False
        assert get(app, f"/api/run/{ITEM}/{FAILED}").status == 404

    def test_a_runs_dir_without_item_is_reported_not_hidden(self, project):
        config = project / ".llm-kit" / "graphs.yaml"
        config.write_text(config.read_text().replace("work/{item}/graph", "work/night-petition/graph"))
        listing = get_json(make_app(project), "/api/runs")
        assert listing["runs"] == [] and "{item}" in listing["error"]


# ---------------------------------------------------------------------------
# escaping
# ---------------------------------------------------------------------------

HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>&amp;'


class TestEscaping:
    def test_markup_in_records_never_leaves_the_server_raw(self, project):
        def edit(record, store):
            node = record["nodes"]["brief"]
            node["attributes"]["sent"] = store.put_blob(HOSTILE)
            node["attributes"]["out"] = store.put_value({"title": HOSTILE, HOSTILE: 1})
            node["error"] = {"type": HOSTILE, "message": HOSTILE}
            node["receipt"] = {"resolved": {"reported_model": HOSTILE, "effort": {"sent": HOSTILE}}, "cost": {"usd": 0.1}}
            record["nodes"][f"poses[{HOSTILE}]/x"] = dict(node, path=f"poses[{HOSTILE}]/x")
            record["nodes"]["plot"]["status"] = HOSTILE

        rewrite_run(project, FAILED, edit)
        app = make_app(project)
        for target in (f"/api/run/{ITEM}/{FAILED}", f"/api/run/{ITEM}/{FAILED}/node?path=brief", "/api/runs"):
            response = get(app, target)
            assert response.status == 200
            assert b"<" not in response.body and b">" not in response.body and b"&" not in response.body
            assert response.header("Content-Type").startswith("application/json")
        d = detail(app, FAILED, "brief")
        assert d["attributes"]["sent"] == HOSTILE            # decoded, so the page can show it as text
        assert d["error"]["message"] == HOSTILE
        ref = RunStore.for_item(load_config(project), ITEM).load_run(FAILED)["nodes"]["brief"]["attributes"]["sent"]
        text = get(app, f"/api/ref/{ITEM}/{ref}")
        assert text.header("Content-Type") == "text/plain; charset=utf-8" and text.header("X-Content-Type-Options") == "nosniff"

    def test_the_page_never_builds_markup_from_text(self):
        banned = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function|setTimeout\(\s*[\"'`]"
                            r"|javascript:|\.srcdoc|createContextualFragment|DOMParser")
        for name in STATIC_FILES:
            text = (STATIC / name).read_text(encoding="utf-8")
            assert not banned.search(text), name
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "<script>" not in html and " onclick" not in html and "style=" not in html

    def test_the_page_stays_ascii_and_has_no_inline_style_or_remote_url(self):
        for name in STATIC_FILES:
            text = (STATIC / name).read_text(encoding="utf-8")
            text.encode("ascii")
            assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), name

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    @pytest.mark.parametrize("name", sorted(n for n in STATIC_FILES if n.endswith(".js")))
    def test_the_modules_parse(self, name):
        result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# the write hook (answer forms arrive with unit L05)
# ---------------------------------------------------------------------------


class TestWriteHook:
    def post(self, app, path="/api/answer", *, origin=f"http://127.0.0.1:{PORT}", token=None, body=b'{"a": 1}',
             content_type="application/json"):
        headers = {**HOST, "Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["X-Viewer-Token"] = token
        return app.handle("POST", path, headers, body)

    def test_the_viewer_has_no_write_route_until_one_is_registered(self, app):
        assert self.post(app).status == 405
        assert app.post_routes == {}

    def test_a_registered_route_needs_the_token_the_origin_and_json(self):
        app = make_app()
        seen = []
        app.add_post("/api/answer", lambda body: seen.append(body) or {"ok": True})
        session = get_json(app, "/api/session")
        assert session["writable"] is True and session["post_routes"] == ["/api/answer"]
        token = session["token"]
        assert self.post(app, token=token).status == 200 and seen == [{"a": 1}]
        assert self.post(app).status == 403                                        # no token
        assert self.post(app, token="wrong").status == 403
        assert self.post(app, token=token, origin=None).status == 403              # no Origin
        assert self.post(app, token=token, origin="http://evil.example").status == 403
        assert self.post(app, token=token, origin=f"http://127.0.0.1:{PORT + 1}").status == 403
        assert self.post(app, token=token, content_type="text/plain").status == 415
        assert self.post(app, token=token, body=b"{nope").status == 400
        assert self.post(app, token=token, body=b"[1]").status == 400
        assert self.post(app, token=token, body=b"x" * (server_module.MAX_POST_BYTES + 1)).status == 413
        assert self.post(app, path="/api/other", token=token).status == 405
        assert seen == [{"a": 1}]

    def test_the_token_is_new_for_every_app(self):
        a, b = make_app(), make_app()
        assert a.token != b.token and len(a.token) >= 40

    @pytest.mark.parametrize("path", ["/other", "api/x", "/api/answer"])
    def test_a_route_must_be_new_and_under_api(self, path):
        app = make_app()
        app.add_post("/api/answer", lambda body: None)
        with pytest.raises(GraphError):
            app.add_post(path, lambda body: None)


# ---------------------------------------------------------------------------
# the loopback server
# ---------------------------------------------------------------------------


@pytest.fixture
def live():
    server = make_server(load_config(FIXTURE), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def request(server, method="GET", target="/api/session", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


class TestLoopbackServer:
    def test_it_binds_the_loopback_address_only(self, live):
        assert live.server_address[0] == "127.0.0.1" and live.server_address[1] > 0

    def test_it_serves_the_page_the_api_and_media(self, live):
        status, headers, body = request(live, target="/")
        assert status == 200 and b"llm-kit graph viewer" in body
        status, _, body = request(live, target="/api/runs")
        assert status == 200 and len(json.loads(body)["runs"]) == 4
        status, headers, body = request(live, target=f"/api/run/{ITEM}/{FAILED}")
        assert status == 200 and headers["X-Content-Type-Options"] == "nosniff"
        status, headers, body = request(live, method="HEAD", target="/")
        assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0

    def test_a_forged_host_header_is_refused(self, live):
        assert request(live, headers={"Host": "attacker.example"})[0] == 403
        assert request(live, headers={"Host": f"127.0.0.1:{live.server_address[1] + 1}"})[0] == 403

    def test_writes_are_refused_over_the_wire(self, live):
        for method in ("POST", "PUT", "DELETE"):
            status, headers, _ = request(live, method=method, target="/api/session", body=b"{}")
            assert status == 405 and headers["Allow"] == "GET, HEAD"

    def test_an_oversize_post_body_closes_the_connection(self, live):
        status, _, _ = request(live, method="POST", target="/api/session", body=b"x" * (server_module.MAX_POST_BYTES + 5))
        assert status == 405


class TestServeCommand:
    def test_serve_is_in_the_verb_table_and_has_a_module(self):
        from llm_kit.cli import graph as graph_cli

        assert "serve" in graph_cli.VERBS
        assert graph_cli.verb_module("serve") == "llm_kit.graph.commands.serve"

    def test_a_project_without_config_exits_1_with_one_line(self, tmp_path, capsys):
        assert serve_command.run(["--project", str(tmp_path)]) == 1
        err = capsys.readouterr().err
        assert err.startswith("llm-kit graph serve: ") and "graphs.yaml" in err and err.count("\n") == 1

    def test_a_busy_port_exits_1(self, capsys):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        try:
            port = blocker.getsockname()[1]
            assert serve_command.run(["--project", str(FIXTURE), "--port", str(port)]) == 1
        finally:
            blocker.close()
        assert "llm-kit graph serve:" in capsys.readouterr().err

    def test_a_bad_port_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            serve_command.run(["--port", "x"])
        assert exc.value.code == 2

    def test_a_missing_viewer_file_exits_1(self, project, capsys):
        (project / "viewer" / "panels.mjs").unlink()
        assert serve_command.run(["--project", str(project)]) == 1
        assert "panels.mjs does not exist" in capsys.readouterr().err

    def test_it_announces_the_address_and_stops_on_interrupt(self, monkeypatch, capsys):
        class Interrupted(server_module._Server):
            def serve_forever(self, *args, **kwargs):
                raise KeyboardInterrupt

        monkeypatch.setattr(server_module, "_Server", Interrupted)
        assert serve_command.run(["--project", str(FIXTURE)]) == 0
        out = capsys.readouterr().out
        assert re.match(r"llm-kit graph viewer: http://127\.0\.0\.1:\d+/", out)
