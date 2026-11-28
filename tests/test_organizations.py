"""Organizations (PHO-283): which one a request acts in, and that the choice is only ever the caller's.

Every account is an organization of one, and an owner can invite teammates,
who then share its phones. A request to the API acts in the organization its
`X-Phone-Harness-Org` header names, or in the caller's own when it has no such
header. This server passes the caller's choice on and makes none itself.
Hosted, the header on a tool call's HTTP request goes on the API requests made
for that call and on no others. Over stdio, PHONE_HARNESS_ORG is read once,
when the server starts. With nothing chosen, nothing new is sent.

`api.request_headers` holds the headers of every request the server made, so
each test here reads what went over the wire. The fake API knows who is in
which organization (`api.orgs`) and refuses everyone else, as the API does.
"""
import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest
import uvicorn
from click.testing import CliRunner
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY

from fake_api import reservation, rid
from phone_harness_mcp import cli, reservations
from phone_harness_mcp.auth import CloudTokenVerifier
from phone_harness_mcp.cloud import CliLogin, Cloud, CloudError, NotAnOrganization, StaticToken, local_org
from phone_harness_mcp.phone import Phones
from phone_harness_mcp.server import build_server, stdio_cloud
from support import connected, start, stdio_server, text_of

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent
HEADER = "X-Phone-Harness-Org"                  # as a client sends it
NAME = HEADER.lower()                           # as the fake API records it
TEAM = "org_0123456789abcdef01234567"           # the caller is an admin here
OTHER = "org_fedcba9876543210fedcba98"          # and only a member here
STRANGERS = "org_aaaaaaaaaaaaaaaaaaaaaaaa"      # a well-formed id of an organization the caller is not in
# Not organization ids: a word, too short, one character short, one too long, capitals, a letter that is not
# hex, two at once (which is how a proxy hands on two headers it has folded into one), and an API key put in
# the wrong place.
NOT_IDS = ["team", "org_123", TEAM[:-1], TEAM + "0", TEAM.upper(), "org_" + "ABCDEF" * 4, "org_" + "g" * 24,
           f"{TEAM},{OTHER}", f"{TEAM}, {OTHER}", "pck_test"]
RESERVE = {"phone_type": "iphone", "plan": "monthly", "use_case": "Testing our listings app on real iPhones."}

# What a client is told, word for word.
NOT_IN_ORG = ("That organization is not one this sign-in is in. Check the X-Phone-Harness-Org header (or "
              "PHONE_HARNESS_ORG), or leave it out to act in this sign-in's own organization.")
OWNER_OR_ADMIN = "Only the organization's owner or an admin can do this."
OWNER_ONLY = "Only the organization's owner can do this."
NOT_AN_ID = ("{} must be an organization id: org_ followed by 24 lowercase hex characters. Leave it out to act in "
             "this sign-in's own organization.")
TWO_HEADERS = ("This request has 2 X-Phone-Harness-Org headers, and the server does not choose between them. Send "
               "one, or none to act in this sign-in's own organization.")


@pytest.fixture
def api(api):
    """The fake API, with the account behind `pck_test` in two organizations besides its own."""
    api.orgs = {TEAM: {"u_1": "admin"}, OTHER: {"u_1": "member"}}
    return api


def failed(tool, sentence):
    """What a client is handed when a tool raises: the SDK names the tool, then the sentence."""
    return f"Error executing tool {tool}: {sentence}"


def named(api, since=0):
    """The organization each request to the API named, in order. None is a request with no such header at all:
    one sent empty would read as ""."""
    return [headers.get(NAME) for headers in api.request_headers[since:]]


def routes(api):
    return [(method, path) for method, path, _bearer in api.requests]


# --- the client: one place adds the header ------------------------------------------

def test_a_client_made_without_an_organization_sends_no_such_header(api):
    Cloud(StaticToken("pck_test"), api.url).request("GET", "/me")
    assert NAME not in api.request_headers[0]


def test_a_client_made_with_one_names_it_on_every_request_beside_a_requests_own_headers(api):
    cloud = Cloud(StaticToken("pck_test"), api.url, org=TEAM)
    cloud.request("GET", "/me")
    cloud.request("GET", "/reservations", headers={"Idempotency-Key": "a-key-of-sixteen-chars"})
    assert named(api) == [TEAM, TEAM]
    sent = api.request_headers[1]
    assert sent["idempotency-key"] == "a-key-of-sixteen-chars" and sent["authorization"] == "Bearer pck_test"


@pytest.mark.parametrize("spelled", [HEADER, NAME])
def test_a_requests_own_headers_never_replace_the_organization(api, spelled):
    cloud = Cloud(StaticToken("pck_test"), api.url, org=TEAM)
    cloud.request("GET", "/me", headers={spelled: OTHER})
    assert named(api) == [TEAM]


def test_a_retry_with_a_fresh_token_names_it_again_and_the_token_request_never_does(api, tmp_path):
    auth = tmp_path / "config" / "auth.json"
    auth.parent.mkdir(parents=True)
    # Still "valid" by its own clock, but the API no longer takes it.
    auth.write_text(json.dumps({"access_token": "at_revoked", "refresh_token": "rt_old",
                                "expires_at": time.time() + 3600}))
    cloud = Cloud(CliLogin(config_dir=tmp_path / "config", issuer=api.url, client_id="cli"), api.url, org=TEAM)
    assert cloud.request("GET", "/me")["email"] == "me@example.com"
    # The organization is the API's business and not the sign-in service's.
    assert list(zip(routes(api), named(api))) == [(("GET", "/me"), TEAM), (("POST", "/oauth/token"), None),
                                                  (("GET", "/me"), TEAM)]


# --- stdio: PHONE_HARNESS_ORG, read once when the server starts -------------------

@pytest.mark.parametrize("value", [None, "", "  "])
def test_an_unset_or_empty_environment_names_no_organization(monkeypatch, value):
    monkeypatch.delenv("PHONE_HARNESS_ORG", raising=False)
    if value is not None:
        monkeypatch.setenv("PHONE_HARNESS_ORG", value)
    assert local_org() is None


def test_the_environment_names_the_organization(monkeypatch):
    monkeypatch.setenv("PHONE_HARNESS_ORG", f" {TEAM}\n")
    assert local_org() == TEAM


@pytest.mark.parametrize("bad", NOT_IDS)
def test_an_environment_value_that_is_not_an_id_is_refused_and_never_repeated(monkeypatch, bad):
    monkeypatch.setenv("PHONE_HARNESS_ORG", bad)
    with pytest.raises(NotAnOrganization) as e:
        local_org()
    assert str(e.value) == NOT_AN_ID.format("PHONE_HARNESS_ORG")
    assert bad not in str(e.value)


class Started:
    """What `main` builds and runs over stdio, without a server behind it."""

    def __init__(self, monkeypatch):
        self.built, self.ran = [], []
        monkeypatch.setattr(cli, "build_server", self.build)

    def build(self, **kw):
        self.built.append(kw)
        return self

    def run(self, transport):
        self.ran.append(transport)


@pytest.mark.parametrize("env, org", [(TEAM, TEAM), (None, None), ("", None)])
def test_the_stdio_server_starts_in_the_organization_the_environment_names(monkeypatch, env, org):
    started = Started(monkeypatch)
    result = CliRunner().invoke(cli.main, ["--api-key", "pck_test", "--api-url", "http://127.0.0.1:9"],
                                env={"PHONE_HARNESS_ORG": env})
    assert result.exit_code == 0, result.output
    assert started.ran == ["stdio"]
    assert started.built[0]["cloud_for"](None).org == org


def test_the_stdio_server_does_not_start_on_a_value_that_is_not_an_id(monkeypatch):
    started = Started(monkeypatch)
    result = CliRunner().invoke(cli.main, ["--api-key", "pck_test"], env={"PHONE_HARNESS_ORG": "org_123"})
    assert result.exit_code == 2
    assert f"Error: {NOT_AN_ID.format('PHONE_HARNESS_ORG')}" in result.output
    assert "org_123" not in result.output
    assert started.built == [] and started.ran == []


def local_server(api, monkeypatch, org):
    """The stdio server wired the way `main` wires it, with PHONE_HARNESS_ORG as given; None is unset."""
    monkeypatch.delenv("PHONE_HARNESS_ORG", raising=False)
    if org is not None:
        monkeypatch.setenv("PHONE_HARNESS_ORG", org)
    return build_server(cloud_for=stdio_cloud(StaticToken("pck_test"), api.url, local_org()), allow_files=True)


async def tour(session):
    """A call to each kind of tool, so that every kind of request the server makes is made. Returns those kinds."""
    sid = await start(session)                  # POST /sessions, then GET /sessions/{id} until the phone is ready
    for tool, args in [("phone_list_available_devices", {}),                       # GET /sessions and GET /me
                       ("phone_take_screenshot", {"device": sid}),                 # POST /sessions/{id}/op
                       ("phone_click_on_screen_at_coordinates", {"device": sid, "x": 5, "y": 6}),
                       ("phone_reserve", RESERVE),                                 # with an Idempotency-Key
                       ("phone_list_reservations", {}),
                       ("phone_list_reservations", {"reservation_id": rid(1)}),
                       ("phone_cancel_reservation", {"reservation_id": rid(1), "confirm": True}),
                       ("phone_stop_device", {"device": sid})]:
        r = await session.call_tool(tool, args)
        assert not r.is_error, text_of(r)
    return {("GET", "/me"), ("GET", "/sessions"), ("POST", "/sessions"), ("GET", f"/sessions/{sid}"),
            ("POST", f"/sessions/{sid}/op"), ("DELETE", f"/sessions/{sid}"), ("POST", "/reservations"),
            ("GET", "/reservations"), ("GET", f"/reservations/{rid(1)}"), ("POST", f"/reservations/{rid(1)}/cancel")}


async def test_stdio_with_nothing_chosen_sends_no_such_header_on_any_request(api, monkeypatch):
    async with connected(local_server(api, monkeypatch, None)) as s:
        kinds = await tour(s)
    assert set(routes(api)) == kinds and len(api.request_headers) == len(api.requests)
    assert all(NAME not in headers for headers in api.request_headers)      # absent, which an empty one is not


async def test_stdio_with_the_environment_set_names_it_on_every_request(api, monkeypatch):
    async with connected(local_server(api, monkeypatch, TEAM)) as s:
        kinds = await tour(s)
    assert set(routes(api)) == kinds
    assert named(api) == [TEAM] * len(api.requests)
    reserving = api.request_headers[routes(api).index(("POST", "/reservations"))]
    assert reserving["idempotency-key"] and reserving[NAME] == TEAM         # the request's own header rides beside it
    assert reserving["authorization"] == "Bearer pck_test"


# --- hosted: the header on a request, for that request -----------------------------

@contextmanager
def hosted(api, **kw):
    """The hosted server on a port of its own. Yields its address."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    args = dict(api=api.url, mcp_path="/mcp", host="127.0.0.1", allowed_hosts=None, allowed_origins=None,
                public_url=None, oauth_issuer=None, stateful=False)
    args.update(kw)
    if args["oauth_issuer"]:
        args["public_url"] = f"http://127.0.0.1:{port}/mcp"
    app = cli.build_http_app(**args)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


def post(base, message, headers=(), path="/mcp"):
    """One JSON-RPC message as one HTTP request that carries exactly `headers` beside the bearer: pairs, so that
    a name can be sent twice. Returns the response's headers and its body."""
    host, port = base.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=30)
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    conn.putrequest("POST", path)
    for name, value in [("Content-Type", "application/json"), ("Accept", "application/json, text/event-stream"),
                        ("Authorization", "Bearer pck_test"), ("Content-Length", str(len(body))), *headers]:
        conn.putheader(name, value)
    conn.endheaders(body)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    assert response.status in (200, 202), (response.status, raw)
    return response.headers, json.loads(raw) if raw else None


def call(base, tool, args=None, headers=(), path="/mcp"):
    """One tool call as one HTTP request. Returns whether it failed, and what it said."""
    _headers, answer = post(base, {"id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}},
                            headers, path)
    result = answer["result"]
    return bool(result.get("isError")), "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def test_hosted_forwards_the_header_for_that_call_and_for_no_other(api):
    seen = []
    with hosted(api) as base:
        for headers in ([(HEADER, TEAM)], [], [(NAME, OTHER)], []):
            mark = len(api.requests)
            error, text = call(base, "phone_list_available_devices", headers=headers)
            assert not error, text
            seen.append(list(zip(routes(api)[mark:], named(api, mark))))
    asked = [("GET", "/sessions"), ("GET", "/me")]                       # every request the call makes, not the first
    assert seen == [[(route, org) for route in asked] for org in (TEAM, None, OTHER, None)]


def test_two_calls_in_flight_each_keep_their_own_organization(api):
    """A phone that is starting is asked after for a couple of seconds. Another caller's call lands in the
    middle of that and names nothing; the first call's later requests still name its organization."""
    with hosted(api) as base:
        starting = threading.Thread(target=call, args=(base, "phone_start_device"),
                                    kwargs={"headers": [(HEADER, TEAM)]})
        starting.start()
        deadline = time.time() + 10
        while ("POST", "/sessions") not in routes(api) and time.time() < deadline:
            time.sleep(0.02)
        error, text = call(base, "phone_list_available_devices")
        starting.join(30)
    assert not error, text
    assert list(zip(routes(api), named(api))) == [
        (("POST", "/sessions"), TEAM), (("GET", "/sessions"), None), (("GET", "/me"), None),
        (("GET", "/sessions/s_0001"), TEAM)]


async def test_hosted_through_an_mcp_client_every_request_names_it(api):
    sid = api.running("android")
    with hosted(api) as base:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer pck_test", HEADER: TEAM}, timeout=30) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, *_rest):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    for tool, args in [("phone_list_available_devices", {}),
                                       ("phone_click_on_screen_at_coordinates", {"device": sid, "x": 5, "y": 6}),
                                       ("phone_reserve", RESERVE)]:
                        r = await s.call_tool(tool, args)
                        assert not r.is_error, text_of(r)
    assert routes(api) == [("GET", "/sessions"), ("GET", "/me"), ("POST", f"/sessions/{sid}/op"),
                           ("POST", "/reservations")]
    assert named(api) == [TEAM] * 4
    assert api.request_headers[-1]["idempotency-key"]       # beside the header a reservation sends for itself


def test_the_second_endpoint_forwards_it_too(api):
    with hosted(api, drive_path="/drive") as base:
        error, text = call(base, "phone_list_available_devices", headers=[(HEADER, TEAM)], path="/drive")
    assert not error, text
    assert named(api) == [TEAM, TEAM]


def test_a_request_with_no_handshake_before_it_is_read_the_same_way(api):
    """A 2026-07-28 client sends each call as one self-contained request, which the SDK serves by another path
    than a session's. The organization is read from that request there too."""
    tool = "phone_list_available_devices"
    envelope = {PROTOCOL_VERSION_META_KEY: "2026-07-28", CLIENT_CAPABILITIES_META_KEY: {}}
    routing = [("MCP-Protocol-Version", "2026-07-28"), ("Mcp-Method", "tools/call"), ("Mcp-Name", tool)]
    seen = []
    with hosted(api) as base:
        for headers in ([(HEADER, TEAM)], [], [(HEADER, TEAM), (HEADER, OTHER)]):
            mark = len(api.requests)
            _headers, answer = post(base, {"id": 1, "method": "tools/call", "params": {
                "name": tool, "arguments": {}, "_meta": envelope}}, routing + headers)
            seen.append((bool(answer["result"].get("isError")), named(api, mark)))
    assert seen == [(False, [TEAM, TEAM]), (False, [None, None]), (True, [])]


def test_one_mcp_session_names_an_organization_on_one_call_and_none_on_the_next(api):
    """`--stateful` keeps an MCP session for each client. The organization is still the request's, not the session's."""
    with hosted(api, stateful=True) as base:
        hello, _answer = post(base, {"id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        session = [("Mcp-Session-Id", hello["mcp-session-id"])]
        post(base, {"method": "notifications/initialized"}, session)
        for org in (TEAM, None, OTHER, None):
            error, text = call(base, "phone_list_reservations", headers=session + ([(HEADER, org)] if org else []))
            assert not error, text
    assert named(api) == [TEAM, None, OTHER, None]


def test_two_organization_headers_fail_the_call_and_nothing_is_sent(api):
    tool = "phone_list_available_devices"
    with hosted(api) as base:
        for pair in ([(HEADER, TEAM), (HEADER, OTHER)], [(HEADER, TEAM), (NAME, TEAM)]):       # even the same one twice
            assert call(base, tool, headers=pair) == (True, failed(tool, TWO_HEADERS)), pair
    assert api.requests == []               # neither was picked, and the call did not go ahead as the caller's own


def test_a_header_that_is_not_an_id_fails_the_call_and_is_neither_sent_nor_repeated(api):
    tool = "phone_list_available_devices"
    sentence = failed(tool, NOT_AN_ID.format("The X-Phone-Harness-Org header"))
    with hosted(api) as base:
        for bad in [*NOT_IDS, ""]:          # a header sent empty names no organization either
            error, text = call(base, tool, headers=[(HEADER, bad)])
            assert (error, text) == (True, sentence), bad
            assert not bad or bad not in text, bad
    assert api.requests == []


@pytest.mark.parametrize("value", [TEAM, "not an id"])
def test_hosted_never_reads_its_own_environment(api, monkeypatch, value):
    """PHONE_HARNESS_ORG is this machine's choice. A hosted server has callers, and each makes their own."""
    monkeypatch.setenv("PHONE_HARNESS_ORG", value)
    with hosted(api) as base:
        error, text = call(base, "phone_list_available_devices")
    assert not error, text
    assert named(api) == [None, None]


# --- the token verifier: is the bearer good, which no organization changes ----------

async def test_the_verifier_asks_with_no_organization_named(api, monkeypatch):
    monkeypatch.setenv("PHONE_HARNESS_ORG", TEAM)
    access = await CloudTokenVerifier(api.url).verify_token("pck_test")
    assert access is not None and access.subject == "u_1"
    assert routes(api) == [("GET", "/me")] and NAME not in api.request_headers[0]


def test_oauth_mode_checks_the_bearer_without_the_header_and_then_forwards_it(api):
    with hosted(api, oauth_issuer="https://issuer.example", oauth_scope="phone:drive") as base:
        for org in (TEAM, OTHER, None):
            error, text = call(base, "phone_list_reservations", headers=[(HEADER, org)] if org else [])
            assert not error, text
    # One `/me`, the verifier's, asked before the first call although that request named an organization. Its
    # answer is remembered by the token alone, so the calls after it name other organizations and it is not asked
    # again.
    assert list(zip(routes(api), named(api))) == [
        (("GET", "/me"), None), (("GET", "/reservations"), TEAM), (("GET", "/reservations"), OTHER),
        (("GET", "/reservations"), None)]


def test_oauth_mode_a_good_bearer_stays_good_whatever_organization_is_named(api):
    tool = "phone_list_reservations"
    with hosted(api, oauth_issuer="https://issuer.example", oauth_scope="phone:drive") as base:
        refused = call(base, tool, headers=[(HEADER, STRANGERS)])
        fine = call(base, tool)
    assert refused == (True, failed(tool, NOT_IN_ORG))      # the tool's answer, not a 401: the bearer was good
    assert fine == (False, "No reservations on this account. phone_reserve starts one.")
    assert named(api) == [None, STRANGERS, None]


# --- what the API refuses, in a sentence ---------------------------------------------

DEVICE = "s_0001"       # the first phone the fake starts
EVERY_KIND_OF_CALL = [
    ("phone_list_available_devices", {}),
    ("phone_start_device", {}),
    ("phone_click_on_screen_at_coordinates", {"device": DEVICE, "x": 1, "y": 2}),
    ("phone_take_screenshot", {"device": DEVICE}),
    ("phone_wake", {"device": DEVICE}),
    ("phone_stop_device", {"device": DEVICE}),
    ("phone_reserve", RESERVE),
    ("phone_list_reservations", {}),
    ("phone_list_reservations", {"reservation_id": rid(1)}),
    ("phone_cancel_reservation", {"reservation_id": rid(1), "confirm": False}),
    ("phone_cancel_reservation", {"reservation_id": rid(1), "confirm": True}),
]


@pytest.mark.parametrize("tool, args", EVERY_KIND_OF_CALL)
async def test_an_organization_the_sign_in_is_not_in_is_said_plainly_by_every_tool(api, tool, args):
    """The API answers 404 on every route. That is not a phone that has stopped, a reservation that does not
    exist, or an API from before reservations, which is what a 404 meant before."""
    assert api.running("ios") == DEVICE                                   # the caller's own phone, and running
    api.hold(reservation(1, status="queued", position=1, paid_days_ago=1))
    async with connected(stdio_server(api, org=STRANGERS)) as s:
        r = await s.call_tool(tool, args)
    assert r.is_error and text_of(r) == failed(tool, NOT_IN_ORG)
    assert len(api.requests) == 1               # asked once: no retry, and no second request to word the refusal
    assert api.reservations[rid(1)]["status"] == "queued" and api.sessions[DEVICE]["state"] == "ready"


async def test_a_member_who_reserves_is_told_who_can(api):
    async with connected(stdio_server(api, org=OTHER)) as s:
        refused = await s.call_tool("phone_reserve", RESERVE)
        listed = await s.call_tool("phone_list_reservations", {})
    assert refused.is_error and text_of(refused) == failed("phone_reserve", OWNER_OR_ADMIN)
    assert 'idempotency_key="' not in text_of(refused)       # nothing was started, so there is nothing to continue
    assert api.reservations == {}
    assert not listed.is_error                               # a member can still look


@pytest.mark.parametrize("body, sentence", [
    ({"code": "role_required", "role": "admin"}, OWNER_OR_ADMIN),
    ({"code": "role_required", "role": "owner"}, OWNER_ONLY),
    ({"code": "role_required"}, OWNER_OR_ADMIN),                                # no role named: the wider one
    ({"error": "Forbidden", "code": "role_required", "role": "owner"}, OWNER_ONLY),
])
def test_a_role_that_is_not_enough_reads_the_same_from_every_route(body, sentence):
    e = CloudError(403, body)
    said = [Phones._translate(e, None), Phones._translate(e, DEVICE), reservations.refusal(e),
            reservations.refusal(e, rid=rid(1)), reservations.refusal(e, key="a-key-of-sixteen-chars")]
    assert [str(s) for s in said] == [sentence] * 5


# --- what did not change, and what the docs say --------------------------------------

async def test_no_tool_takes_an_organization(api):
    """The choice comes with the request or the environment, never as a tool's argument: the tools are as they were."""
    async with connected(stdio_server(api)) as s:
        tools = (await s.list_tools()).tools
    assert tools
    for tool in tools:
        assert "org" not in json.dumps(tool.input_schema).lower(), tool.name


def test_the_docs_say_how_an_organization_is_named():
    readme = " ".join((ROOT / "README.md").read_text().split())
    section = readme.split("## Organizations", 1)[1].split(" ## ", 1)[0]
    assert "`X-Phone-Harness-Org`" in section and "`PHONE_HARNESS_ORG`" in section
    assert '"headers": { "X-Phone-Harness-Org": "org_' in section
    assert "An API key belongs to one organization" in section
    assert "cannot send a header acts in your own organization" in section
    hosting = " ".join((ROOT / "docs" / "self-hosting.md").read_text().split())
    assert "`X-Phone-Harness-Org`" in hosting and '"PHONE_HARNESS_ORG": "org_' in hosting
    unreleased = (ROOT / "CHANGELOG.md").read_text().split("## 0.1.0")[0]
    assert "`X-Phone-Harness-Org`" in unreleased and "`PHONE_HARNESS_ORG`" in unreleased
